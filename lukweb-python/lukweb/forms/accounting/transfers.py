import logging
import re
from collections import defaultdict
from decimal import Decimal
from itertools import chain

from django.utils.translation import (
    ugettext_lazy as _,
)
from djmoney.money import Money

from ... import payments, models
from . import internal, ticketing, bulk_utils
from ..utils import ParserErrorMixin
from django.conf import settings

logger = logging.getLogger(__name__)

__all__ = [
    'ElectronicPaymentPopulator', 'BankCSVParser'
]


# TODO: implement KBC parser
# TODO: implement parser switching in globals
# TODO: clearly document parsers
# TODO: delimiter autodetection

class BankCSVParser(bulk_utils.PaymentCSVParser):
    
    class TransactionInfo(PaymentCSVParser.TransactionInfo): 
        def __init__(self, *, ogm, **kwargs):
            super().__init__(**kwargs)
            self.ogm = ogm

    def get_nature(self, line_no, row):
        return payments.PAYMENT_NATURE_TRANSFER

    def get_ogm(self, line_no, row):
        raise NotImplementedError

    def parse_row_to_dict(self, line_no, row):
        parsed = super().parse_row_to_dict(line_no, row)
        ogm = self.get_ogm(line_no, row)
        if ogm is None:
            return None
        parsed['ogm'] = ogm
        return parsed


# lookbehind doesn't work, since we don't want to constrain the
# prefix to a fixed length
FORTIS_FIND_OGM = r'MEDEDELING\s*:\s+' + payments.OGM_REGEX
FORTIS_SEARCH_PATTERN = re.compile(FORTIS_FIND_OGM)


class FortisCSVParser(BankCSVParser):
    delimiter = ';'

    # TODO: force all relevant columns to be present here
    amount_column_name = 'Bedrag'
    date_column_name = 'Uitvoeringsdatum'

    def get_ogm(self, line_no, row):
        m = FORTIS_SEARCH_PATTERN.search(row['Details'])
        if m is None:
            return None
        ogm_str = m.group(0)
        try:
            prefix, modulus = payments.parse_ogm(ogm_str, match=m)
        except (ValueError, TypeError):
            self.error(
                line_no, 
                _('Illegal OGM string %(ogm)s.') % {
                    'ogm': ogm_str
                }
            )
            return None

        ogm_canonical = payments.ogm_from_prefix(prefix)
        return ogm_canonical


class TransferRecordPreparator(bulk_utils.LedgerEntryPreparator):
    
    prefix_digit = None

    def ogm_applies(self, ogm):
        try:
            prefix, modulus = payments.parse_ogm(ogm)
            return self.prefix_digit == str(prefix)[0]:
        except ValueError:
            return False

    def model_kwargs_for_transaction(self, transaction):
        if not self.ogm_applies(transaction.ogm):
            return None
        return super().model_kwargs_for_transaction(transaction)


class DebtTransferPaymentPreparator(TransferRecordPreparator,
                                    bulk_utils.DuplicationProtectedPreparator,
                                    bulk_utils.CreditApportionmentMixin):

    model = models.InternalPayment
    formset_class = internal.BaseBulkPaymentFormSet
    split_model = models.InternalPaymentSplit
    formset_prefix = 'bulk-debt-transfers'
    prefix_digit = payments.OGM_INTERNAL_DEBT_PREFIX

    multiple_dup_message = _(
        'A bank transfer payment by %(member)s '
        'for amount %(amount)s on date %(date)s appears %(hist)d time(s) '
        'in history, and %(import)d time(s) in '
        'the current batch of data. '
        'Resolution: %(dupcount)d ruled as duplicate(s).'
    )

    single_dup_message = _(
        'A bank transfer payment by %(member)s '
        'for amount %(amount)s on date %(date)s already appears '
        'in the payment history. '
        'Resolution: likely duplicate, skipped processing.'
    )

    overpayment_fmt_string = _(
        'Not all bank transfer payments of %(member)s can be fully utilised. '
        'Received %(total_credit)s, but only %(total_used)s '
        'can be applied to outstanding debts.'
    )

    def overpayment_error_params(self, debt_key, total_used, total_credit): 
        return {
            'member': str(self._members_by_id[debt_key]),
            'total_used': total_used,
            'total_credit': total_credit
        }

    def model_kwargs_for_transaction(self, transaction):
        kwargs = super().model_kwargs_for_transaction(transaction)
        if kwargs is None:
            return None
        pk = parse_internal_debt_ogm(transaction.ogm)
        member = self._members_by_id[pk]
        # the pk part might match accidentally
        # so we check the hidden token digest too.
        # This shouldn't really matter all that much 
        # in the current implementation, but it can't hurt.
        if transaction.ogm != member.payment_tracking_no:
            return None
        kwargs['member'] = member
        kwargs['nature'] = payments.PAYMENT_NATURE_TRANSFER
        return kwargs

    def form_kwargs_for_transaction(self, transaction):
        kwargs = super().form_kwargs_for_transaction(transaction)
        member = transaction.ledger_entry.member
        kwargs['member_id'] = member.pk
        kwargs['name'] = member.full_name
        kwargs['email'] = member.user.email
        kwargs['nature'] = payments.PAYMENT_NATURE_TRANSFER
        return kwargs 

    def prepare(self):
        ogms_to_query = [
            t.ogm for t in self.transactions if self.ogm_applies(t.ogm)
        ]

        member_qs, unseen = models.ChoirMember.objects.with_debt_balances()\
            .select_related('user').by_payment_tracking_nos(
                ogms_to_query, validate_unseen=True
            )

        # We don't show this error in the interface, since
        # if the OGM validates properly AND is not found in our system,
        # it probably simply corresponds to a transaction that we don't
        # care about
        logger.debug(
            'OGMs not corresponding to valid user records: %s.', 
            ', '.join(unseen)
        )

        self._members_by_id = {
            member.pk: member for member in member_qs
        }

    def transaction_buckets(self):
        trans_buckets = defaultdict(list)
        for t in self.valid_transactions:
            member_id = t.ledger_entry.member.pk
            trans_buckets[member_id].append(t)
        debt_qs = models.InternalDebtItem.objects.filter(
            member_id__in=self._members_by_id.keys()
        ).with_payments().unpaid().order_by('timestamp')

        debt_buckets = defaultdict(list)
        for debt in debt_qs:
            debt_buckets[debt.member_id].append(debt)

        self._debt_buckets = debt_buckets

        return trans_buckets

    def debts_for(self, debt_key):
        return self._debt_buckets[debt_key]
