import datetime
import logging
from csv import DictReader
from decimal import Decimal, DecimalException
from collections import defaultdict, namedtuple, deque
from typing import TypeVar, Sequence, Generator, Type

from django import forms
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import (
    ugettext_lazy as _,
)
from djmoney.money import Money

from ... import models
from ...models.accounting import base as accounting_base
from ...utils import _dt_fallback
from ..utils import ParserErrorMixin, CSVUploadForm

logger = logging.getLogger(__name__)

"""
Utilities for processing & displaying accounting data
originating from .csv files
"""
# TODO (wbc): user-configurable column names, or at least
# translatable ones
# TODO: unified method to mark required/optional columns

class FinancialCSVParser:
    delimiter = ','
    amount_column_name = 'bedrag'
    date_column_name= 'datum'

    class TransactionInfo:
        def __init__(self, *, line_no, amount, timestamp):
            self.ledger_entry = None
            self.line_no = line_no
            self.amount = amount
            self.timestamp = _dt_fallback(timestamp)

    def __init__(self, csv_file):
        self.csv_file = csv_file
        self._file_read = False
        self._errors = []
        self._objects = []

    def error(self, line_no, msg):
        self._errors.insert(0, (line_no, msg))

    def parse_row(self, line_no, row):
        kwargs = self.parse_row_to_dict(line_no, row)
        if kwargs is None:
            return None
        return self.__class__.TransactionInfo(
            **kwargs
        )

    def parse_row_to_dict(self, line_no, row):
        amount = self._parse_amount(
            line_no, row[self.amount_column_name]
        )
        date_str = row.get(self.date_column_name, None)
        if not date_str:
            timestamp = timezone.now()
        else:
            timestamp = self._parse_date(line_no, date_str)

        if timestamp is None or amount is None:
            return None
        return {
            'amount': amount,
            'timestamp': timestamp,
            'line_no': line_no
        }

    @property
    def errors(self):
        if not self._file_read:
            self._read()
        return self._errors

    @property
    def parsed_data(self):
        if not self._file_read:
            self._read()
        return self._objects

    def _read(self):
        if self.csv_file is None:
            self._file_read = True
            return

        csv = DictReader(self.csv_file, delimiter=self.delimiter)

        def gen():
            for line_no, row in enumerate(csv):
                # +1 to offset zero-indexing, and +1 to skip the header
                t = self.parse_row(line_no + 2, row)
                if t is not None:
                    yield t

        try:
            self._objects = list(gen())
        except KeyError as e:
            from django.utils.translation import ugettext as _
            self.error(
                0, _('Missing column: %(col)s. No data processed.') % {
                    'col': e.args[0]
                }
            )
        self._file_read = True

    def _parse_amount(self, line_no, amount_str):

        # ugly, but Decimal doesn't really support formatting parameters
        # (unless we involve the locale module)
        amt_str = amount_str.replace(',', '.')
        # even though currency may be available in the input row
        # we still force EUR, since the data model can't handle
        # anything else
        currency = settings.BOOKKEEPING_CURRENCY

        try:
            rd = Decimal(amt_str).quantize(Decimal('.01'))
            return Money(rd, currency)
        except (ValueError, IndexError, DecimalException):
            self.error(
                line_no,
                _('Invalid amount %(amt)s') % {
                    'amt': amt_str
                },
            )
            return None

    def _parse_date(self, line_no, date_str):
        try:
            return datetime.datetime.strptime(
                date_str, '%d/%m/%Y'
            ).date()
        except ValueError:
            self.error(
                line_no,
                _('Invalid date %(date)s, please use dd/mm/YYYY.') % {
                    'date': date_str
                },
            )
            return None


class MemberTransactionParser(FinancialCSVParser):
    member_column_name = 'lid'

    class TransactionInfo(FinancialCSVParser.TransactionInfo):
        def __init__(self, *, member_str, **kwargs):
            super().__init__(**kwargs)
            self.member_str = member_str
    
    def parse_row_to_dict(self, line_no, row):
        parsed = super().parse_row_to_dict(line_no, row)
        parsed['member_str'] = row[self.member_column_name]
        return parsed


class PaymentCSVParser(FinancialCSVParser):

    class TransactionInfo(FinancialCSVParser.TransactionInfo): 
        def __init__(self, *, nature, **kwargs):
            super().__init__(**kwargs)
            self.nature = int(nature)

    def get_nature(self, line_no, row):
        raise NotImplementedError

    def parse_row_to_dict(self, line_no, row):
        parsed = super().parse_row_to_dict(line_no, row)
        nature = self.get_nature(line_no, row)
        if nature is None:
            return None
        parsed['nature'] = nature
        return parsed


class LedgerEntryPreparator(ParserErrorMixin):
    model = None 
    formset_class = None
    formset_prefix = None
    _valid_transactions = None
    _formset = None

    def __init__(self, parser):
        super().__init__(parser)
        if parser is not None:
            self.transactions = parser.parsed_data
        else:
            self.transactions = []

    def error_at_line(self, line_no, msg, params=None):
        self.error_at_lines([line_no], msg, params)

    def error_at_lines(self, line_nos, msg, params):
        fmtd_msg = msg % params
        self._errors.insert(0, (sorted(line_nos), fmtd_msg))

    # The prepare/review methods are called before
    # and after transaction validation, respectively.
    # The review method can access and use
    # self.valid_transactions.
    def prepare(self):
        return

    def review(self):
        return

    def model_kwargs_for_transaction(self, transaction):
        # validate and build model kwargs for transaction
        if transaction.amount.amount < 0:
            self.error_at_line(
                transaction.line_no,
                _('Payment amount %(amount)s is negative.'),
                params={'amount': transaction.amount}
            )
            return None

        return {
            'total_amount': transaction.amount,
            'timestamp': transaction.timestamp
        } 

    def validate_global(self, valid_transactions):
        # this method can assume that all transactions have the
        # ledger_entry property set to something meaningful
        return valid_transactions

    @property
    def valid_transactions(self): 
        if self._valid_transactions is None:
            self.prepare()
            def valid(t):
                kwargs = self.model_kwargs_for_transaction(t)
                if kwargs is not None:
                    t.ledger_entry = self.model(**kwargs)
                    return True
                else:
                    return False
            indiv_transactions = [
                t for t in self.transactions if valid(t)
            ]
            self._valid_transactions = self.validate_global(indiv_transactions)
            self.review()

        return self._valid_transactions

    def form_kwargs_for_transaction(self, transaction):
        if self._valid_transactions is None:
            raise ValueError(
                'Ledger entries are not ready.'
            )
        return {
            'total_amount': transaction.amount,
            'timestamp': transaction.timestamp
        }

    def formset_kwargs(self):
        return {}

    def run(self):
        initial_data = [
            self.form_kwargs_for_transaction(t)
            for t in self.valid_transactions
        ]
        fs = self.formset_class(
            queryset=self.model._default_manager.none(),
            initial=initial_data,
            prefix=self.formset_prefix,
            **self.formset_kwargs()
        )
        fs.extra = len(self.valid_transactions)
        self._formset = fs

    @property
    def formset(self):
        if self._formset is None:
            self.run()
        return self._formset


class FetchMembersMixin(LedgerEntryPreparator):

    _members_by_str = None
    _members_by_id = None

    def unknown_member(self, member_str, line_nos):
        msg = _(
            '%(member_str)s does not designate a registered member.'
        )

        self.error_at_lines(
            line_nos, msg, params={'member_str': member_str}
        )

    def get_member(self, pk=None, member_str=None):
        if pk is not None:
            return self._members_by_id[pk]
        elif member_str is not None:
            return self._members_by_str[member_str]
        raise ValueError('You must supply either pk or member_str')

    def member_ids(self):
        return self._members_by_id.keys()

    def prepare(self): 
        super().prepare()
        # split the transaction list into email and name indices
        email_index, name_index = defaultdict(list), defaultdict(list)
        for info in self.transactions:
            use_email = '@' in info.member_str
            targ = email_index if use_email else name_index
            key = info.member_str if use_email else info.member_str.casefold()
            targ[key].append(info)

        member_email_qs, unseen = models.ChoirMember.objects \
            .select_related('user').with_debt_annotations().by_emails(
                email_index.keys(), validate_unseen=True
            )

        for email in unseen:
            ts = email_index[email]
            self.unknown_member(email, [t.line_no for t in ts])

        # TODO Restrict to active members only, maybe?
        member_name_qs, unseen, duplicates = models.ChoirMember.objects \
            .select_related('user').with_debt_annotations().by_full_names(
                name_index.keys(), validate_unseen=True, validate_nodups=True
            )

        for name in unseen:
            ts = name_index[name.casefold()]
            self.unknown_member(name, [t.line_no for t in ts])

        for name in duplicates:
            ts = name_index[name.casefold()]
            msg = _(
                '%(member_str)s designates multiple registered members. '
                'Skipped processing.',
            )
            self.error_at_lines(
                [t.line_no for t in ts], msg, params={'member_str': name},
            )

        self._members_by_str = dict()
        self._members_by_id = dict()

        # build member dictionaries
        for m in member_email_qs:
            member_str = m.user.email
            self._members_by_str[member_str] = m
            self._members_by_id[m.pk] = m

        for m in member_name_qs:
            member_str = m.full_name
            imember_str = member_str.casefold()
            if imember_str not in duplicates:
                self._members_by_str[member_str] = m
                self._members_by_id[m.pk] = m

        # It's technically more efficient to keep the transaction dicts around
        # to refer to later, but since later calls to validate_global might
        # shrink the list of valid transactions, this is a bad idea for 
        # maintainability. Amdahl.

    # TODO: In the long term I would like to get rid of these eph
    # forms as well. That should be a bit easier to plan with the
    # new bulk_utils module.
    def form_kwargs_for_transaction(self, transaction):
        kwargs = super().form_kwargs_for_transaction(transaction)
        member = transaction.ledger_entry.member
        kwargs['member_id'] = member.pk
        kwargs['name'] = member.full_name
        kwargs['email'] = member.user.email
        return kwargs
    
    def model_kwargs_for_transaction(self, transaction):
        kwargs = super().model_kwargs_for_transaction(transaction)
        if kwargs is None:
            return None
        try:
            member = self._members_by_str[transaction.member_str]
            kwargs['member'] = member
            return kwargs
        except KeyError: 
            # member search errors have already been logged
            # in the preparation step, so we don't care
            return None


class DuplicationProtectedPreparator(LedgerEntryPreparator):
    single_dup_message = None
    multiple_dup_message = None

    def validate_global(self, valid_transactions):
        valid_transactions = super().validate_global(valid_transactions)
        # early out, nothing to do
        if not valid_transactions:
            return []
        dates = [
            timezone.localdate(t.timestamp) for t in valid_transactions
        ]

        historical_buckets = self.model._default_manager.dupcheck_buckets(
            date_bounds=(min(dates), max(dates))
        )

        import_buckets = defaultdict(list)
        for transaction in valid_transactions:
            sig = transaction.ledger_entry.dupcheck_signature
            import_buckets[sig].append(transaction)

        def strip_duplicates():
            for dup_sig, transactions in import_buckets.items():
                occ_in_import = len(transactions)
                occ_in_hist = historical_buckets[dup_sig]
                dupcount = min(occ_in_hist, occ_in_import)
                if occ_in_hist:
                    # signal duplicate with an error message
                    params = self.dup_error_params(dup_sig)
                    params['hist'] = occ_in_hist
                    params['import'] = occ_in_import
                    params['dupcount'] = dupcount

                    # special case, this is the most likely one to occur
                    # so deserves special wording
                    if occ_in_hist == occ_in_import == 1:
                        msg_fmt_str = self.single_dup_message
                    else:
                        msg_fmt_str = self.multiple_dup_message
                    # report duplicate error
                    self.error_at_lines(
                        [t.line_no for t in transactions],
                        msg_fmt_str,
                        params=params
                    )
                # skip the first dupcount entries, we treat those as the 
                # duplicate ones. The others will be entered into the db
                # as usual
                yield from transactions[dupcount:]

        return list(strip_duplicates())
                

    def dup_error_params(self, signature_used):
        return {
            'date': signature_used[0],
            'amount': Money(signature_used[1], settings.BOOKKEEPING_CURRENCY),
        }


ApportionmentResult = namedtuple(
    'ApportionmentResult', (
        'fully_used_payments',
        'fully_paid_debts',
        'remaining_payments',
        'remaining_debts'
    )
)


ST = TypeVar('ST', bound=accounting_base.BaseDebtPaymentSplit)
def make_payment_splits(payments: Sequence[accounting_base.BasePaymentRecord],
                        debts: Sequence[accounting_base.BaseDebtRecord],
                        split_model: Type[ST],
                        prioritise_exact_amount_match=True,
                        exact_amount_match_only=False,
                        payment_fk_name: str=None, debt_fk_name: str=None) \
        -> Generator[ST, None, ApportionmentResult]:
    """
    This method assumes that there are no preexistent splits between
    the payments and debts involved in the computation.
    Ensure that the payments and debts are appropriately annotated for
    optimal results.
    """

    # use double-ledger introspection to figure out the right foreign
    # key names
    if payment_fk_name is None:
        payment_fk_name = split_model.get_payment_column()
    if debt_fk_name is None:
        debt_fk_name = split_model.get_debt_column()

    results = ApportionmentResult(
        fully_used_payments=[],
        fully_paid_debts=[],
        remaining_payments=[],
        remaining_debts=[]
    )

    # There might be a more efficient way, but let's not optimise prematurely
    if prioritise_exact_amount_match or exact_amount_match_only:
        payment_list = list(payments)
        debt_list = deque(debts)
        payments_todo = []
        for payment in payment_list:
            try:
                amt = payment.credit_remaining
                # attempt to find a debt matching the exact payment amount
                index, exact_match = next(
                    (ix, d) for ix, d in enumerate(debt_list)
                    if d.balance == amt and d.timestamp <= payment.timestamp
                )
                # remove debt from the candidate list
                # this triggers another O(n) read, which feels like
                # it should be unnecessary, but it doesn't really matter
                del debt_list[index]

                # yield payment split covering this transaction
                yield split_model(**{
                    payment_fk_name: payment, debt_fk_name: exact_match,
                    'amount': amt
                })
                results.fully_used_payments.append(payment)
                results.fully_paid_debts.append(exact_match)
            except StopIteration:
                # no exact match, so defer handling
                payments_todo.append(payment)

        debts_iter = iter(debt_list)
        payments_iter = iter(payments_todo)
    else:
        payments_iter = iter(payments)
        debts_iter = iter(debts)

    if exact_amount_match_only:
        results.remaining_debts.extend(debts_iter)
        results.remaining_payments.extend(payments_iter)
        return results

    # The generic method is simple: use payments to pay off debts
    # until we either run out of debts, or of money to pay 'em
    payment = debt = None
    # credit remaining on the current payment
    # and portion of the current debt that still needs to be paid
    # (during this payment run)

    # The only subtlety is in enforcing the invariant
    # that debts cannot be retroactively paid off by past payments.
    # By ordering the payments and debts from old to new, we can easily
    # ensure that this happens.
    credit_remaining = debt_remaining = 0
    while True:
        try:
            # look for some unpaid debt
            while not debt_remaining:
                if debt is not None:  # initial step guard
                    # report debt as fully paid
                    results.fully_paid_debts.append(debt)
                debt = next(debts_iter)
                debt_remaining = debt.balance
        except StopIteration:
            # all debts fully paid back, bail
            if credit_remaining:
                results.remaining_payments.append(payment)
            break

        try:
            # keep trying payments until we find one that is recent enough
            # to cover the current debt.
            while not credit_remaining or payment.timestamp < debt.timestamp:
                if payment is not None:  # initial step guard
                    # report on payment status
                    if credit_remaining:
                        results.remaining_payments.append(payment)
                    else:
                        results.fully_used_payments.append(payment)
                payment = next(payments_iter)
                credit_remaining = payment.credit_remaining

        except StopIteration:
            # no money left to pay stuff, bail
            if debt_remaining:
                results.remaining_debts.append(debt)
            break
        # pay off as much of the current debt as we can
        # with the current balance
        amt = min(debt_remaining, credit_remaining)
        credit_remaining -= amt
        debt_remaining -= amt
        yield split_model(**{
            payment_fk_name: payment, debt_fk_name: debt, 'amount': amt
        })

    return results


class CreditApportionmentMixin(LedgerEntryPreparator):
    split_model = None
    overpayment_fmt_string = None

    # optional, can be derived through reflection
    payment_fk_name = None
    debt_fk_name = None
    _trans_buckets = None

    def debts_for(self, debt_key):
        raise NotImplementedError

    def transaction_buckets(self):
        raise NotImplementedError

    def overpayment_error_params(self, debt_key, total_used, total_credit): 
        return {
            'debt_key': debt_key,
            'total_used': total_used,
            'total_credit': total_credit
        }

    def make_splits(self, payments, debts):
        return make_payment_splits(
            payments, debts, self.split_model, 
            payment_fk_name=self.payment_fk_name,
            debt_fk_name=self.debt_fk_name
        )

    def review(self):
        super().review()
        # compute the total credit used vs the total
        # credit established, and notify the treasurer
        self._trans_buckets = self.transaction_buckets()
        for key, transactions in self._trans_buckets.items():
            debts = self.debts_for(key) 
            payments = sorted([
                    t.ledger_entry for t in transactions
                ], 
                key=lambda p: p.timestamp
            )
            splits = self.make_splits(payments, debts)
            total_used = sum(
                (s.amount for s in splits),
                Money(0, settings.BOOKKEEPING_CURRENCY)
            )
            total_credit = sum(
                (p.total_amount for p in payments),
                Money(0, settings.BOOKKEEPING_CURRENCY)
            )
            if total_used < total_credit:
                self.error_at_lines(
                    [t.line_no for t in transactions],
                    self.overpayment_fmt_string,
                    params=self.overpayment_error_params(
                        key, total_used, total_credit
                    )
                )
                # TODO: figure out how to deal with refunds here
                # I'm convinced again that having a to_refund account in gnucash
                # is the best way to deal with this, but we need to
                # have a qif_excluded field on payments in case the treasurer
                # wants to deal with these things manually


class FinancialCSVUploadForm(CSVUploadForm):
    ledger_preparator_classes = ()
    upload_field_label = None
    csv_parser_class = None

    csv = forms.FileField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['csv'].label = self.upload_field_label
    
    @cached_property
    def formset_preparators(self):
        data = self.cleaned_data['csv']
        return tuple(
            prep(data) for prep in self.ledger_preparator_classes
        )

    # is the most common use case
    @property
    def formset_preparator(self):
        assert len(self.ledger_preparator_classes) == 1
        return self.formset_preparators[0]
 
    def render_confirmation_page(self, request, context=None):
        raise NotImplementedError

    @classmethod
    def submit_confirmation(cls, post_data):
        # call save() on the right formsets after user confirmation
        formsets = [
            prep.formset_class(post_data, prefix=prep.formset_prefix)
            for prep in cls.ledger_preparator_classes 
        ]

        dirty = False
        for formset in formsets:
            # this means someone tampered with the POST data,
            # so we have no obligation to give a nice response
            if not formset.is_valid():
                logger.error(formset.errors)
                dirty = True

        if dirty:
            raise SuspiciousOperation()

        with db_transaction.atomic():
            for formset in formsets:
                formset.save()
