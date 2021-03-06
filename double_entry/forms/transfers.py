import logging
from collections import defaultdict
from typing import Dict, TypeVar, Optional

import double_entry.utils
from double_entry import models
from double_entry.forms.csv import TransactionInfo, BankTransactionInfo
from double_entry.models import TransactionPartyMixin
from double_entry.forms import bulk_utils

logger = logging.getLogger(__name__)

# TODO: clearly document parsers
# TODO: delimiter autodetection


TP = TypeVar('TP', bound=TransactionPartyMixin)
TI = TypeVar('TI', bound=TransactionInfo)
RT = TypeVar('RT', bound=bulk_utils.ResolvedTransaction)
LE = TypeVar('LE', bound=models.DoubleBookModel)

class TransferTransactionIndexBuilder(bulk_utils.TransactionPartyIndexBuilder[TP]):
    prefix_digit: int

    def __init__(self, resolver: bulk_utils.LedgerResolver, prefix_digit: int):
        self.account_index: Dict[str, TP] = {}
        self.line_index = defaultdict(list)
        self.prefix_digit = prefix_digit
        super().__init__(resolver)

    def lookup(self, account_lookup_str: str) -> Optional[TP]:
        try:
            double_entry.utils.normalise_ogm(account_lookup_str)
        except ValueError:
            return None
        return self.account_index.get(account_lookup_str)

    @classmethod
    def lookup_key_for_account(cls, account):
        return account.payment_tracking_no

    def ogm_applies(self, ogm):
        try:
            prefix, modulus = double_entry.utils.parse_ogm(ogm)
            return self.prefix_digit == prefix // 10**9
        except ValueError:
            return False

    def append(self, tinfo):
        string = tinfo.account_lookup_str
        if not self.ogm_applies(string):
            return False
        else:
            self.line_index[string].append(tinfo.line_no)
            return True

    def execute_query(self):
        account_qs, unseen = self.base_query_set().by_payment_tracking_nos(
            self.line_index.keys(), validate_unseen=True
        )

        if unseen:
            self.report_invalid_ogms(unseen)
        for m in account_qs:
            self.account_index[m.payment_tracking_no] = m

    def report_invalid_ogms(self, unseen):
        """
        Separate for easier mocking in tests.
        We don't show this error in the interface, since
        if the OGM validates properly AND is not found in our system,
        it probably simply corresponds to a transaction that we don't
        care about.
        """
        logger.info(
            'OGMs not corresponding to valid user records: %s.',
            ', '.join(unseen)
        )


class TransferResolver(bulk_utils.LedgerResolver[TP, TI, RT], abstract=True):
    transaction_info_class = BankTransactionInfo

    def get_index_builders(self):
        tpm = self.__class__.transaction_party_model
        prefix_digit = tpm.payment_tracking_prefix
        return [
            TransferTransactionIndexBuilder(self, prefix_digit=prefix_digit)
        ]
