import datetime
from collections import OrderedDict
from enum import Enum
from logging import getLogger
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from django.contrib.postgres.fields import ArrayField, JSONField
from django.db import models
from django.db.models import Case, Q, QuerySet, Sum
from django.db.models.expressions import (F, OuterRef, RawSQL, Subquery, Value,
                                          When)
from django.db.models.signals import post_save

from hexbytes import HexBytes
from model_utils.models import TimeStampedModel

from gnosis.eth import EthereumClientProvider
from gnosis.eth.constants import ERC20_721_TRANSFER_TOPIC
from gnosis.eth.django.models import (EthereumAddressField, HexField,
                                      Sha3HashField, Uint256Field)
from gnosis.safe import SafeOperation

from .utils import clean_receipt_log

logger = getLogger(__name__)


class ConfirmationType(Enum):
    CONFIRMATION = 0
    EXECUTION = 1


class EthereumTxCallType(Enum):
    CALL = 0
    DELEGATE_CALL = 1

    @staticmethod
    def parse_call_type(call_type: str):
        if not call_type:
            return None
        elif call_type.lower() == 'call':
            return EthereumTxCallType.CALL
        elif call_type.lower() == 'delegatecall':
            return EthereumTxCallType.DELEGATE_CALL
        else:
            return None


class EthereumTxType(Enum):
    CALL = 0
    CREATE = 1
    SELF_DESTRUCT = 2

    @staticmethod
    def parse(tx_type: str):
        tx_type = tx_type.upper()
        if tx_type == 'CALL':
            return EthereumTxType.CALL
        elif tx_type == 'CREATE':
            return EthereumTxType.CREATE
        elif tx_type == 'SUICIDE':
            return EthereumTxType.SELF_DESTRUCT
        else:
            raise ValueError(f'{tx_type} is not a valid EthereumTxType')


class TransactionNotFoundException(Exception):
    pass


class TransactionWithoutBlockException(Exception):
    pass


class BulkCreateSignalMixin:
    def bulk_create(self, objs, **kwargs):
        result = super().bulk_create(objs, **kwargs)
        for obj in objs:
            post_save.send(obj.__class__, instance=obj, created=True)
        return result


class EthereumBlockManager(models.Manager):
    def get_or_create_from_block_number(self, block_number: int):
        try:
            return self.get(number=block_number)
        except self.model.DoesNotExist:
            ethereum_client = EthereumClientProvider()
            current_block_number = ethereum_client.current_block_number  # For reorgs
            block = ethereum_client.get_block(block_number)
            return self.create_from_block(block, current_block_number=current_block_number)

    def get_or_create_from_block(self, block: Dict[str, Any], current_block_number: Optional[int] = None):
        try:
            return self.get(number=block['number'])
        except self.model.DoesNotExist:
            return self.create_from_block(block, current_block_number=current_block_number)

    def create_from_block(self, block: Dict[str, Any], current_block_number: Optional[int] = None) -> 'EthereumBlock':
        # If confirmed, we will not check for reorgs in the future
        confirmed = (current_block_number - block['number']) >= 6 if current_block_number is not None else False
        return super().create(
            number=block['number'],
            gas_limit=block['gasLimit'],
            gas_used=block['gasUsed'],
            timestamp=datetime.datetime.fromtimestamp(block['timestamp'], datetime.timezone.utc),
            block_hash=block['hash'],
            parent_hash=block['parentHash'],
            confirmed=confirmed,
        )


class EthereumBlockQuerySet(models.QuerySet):
    def not_confirmed(self, to_block_number: Optional[int] = None):
        queryset = self.filter(confirmed=False)
        if to_block_number is not None:
            queryset = queryset.filter(number__lte=to_block_number)
        return queryset.order_by('number')


class EthereumBlock(models.Model):
    objects = EthereumBlockManager.from_queryset(EthereumBlockQuerySet)()
    number = models.PositiveIntegerField(primary_key=True, unique=True)
    gas_limit = models.PositiveIntegerField()
    gas_used = models.PositiveIntegerField()
    timestamp = models.DateTimeField()
    block_hash = Sha3HashField(unique=True)
    parent_hash = Sha3HashField(unique=True)
    # For reorgs, True if `current_block_number` - `number` >= MIN_CONFIRMATIONS
    confirmed = models.BooleanField(default=False,
                                    db_index=True)

    def __str__(self):
        return f'Block number={self.number} on {self.timestamp}'

    def set_confirmed(self):
        self.confirmed = True
        self.save()


class EthereumTxManager(models.Manager):
    def __update_with_receipt_and_block(self, ethereum_tx: 'EthereumTx', ethereum_block: 'EthereumBlock',
                                        tx_receipt: Dict[str, Any]):
        if ethereum_tx.block is None:
            ethereum_tx.block = ethereum_block
            ethereum_tx.gas_used = tx_receipt['gasUsed']
            ethereum_tx.logs = [clean_receipt_log(log) for log in tx_receipt['logs']]
            ethereum_tx.status = tx_receipt.get('status')
            ethereum_tx.transaction_index = tx_receipt['transactionIndex']
            ethereum_tx.save(update_fields=['block', 'gas_used', 'logs', 'status', 'transaction_index'])
        return ethereum_tx

    def create_or_update_from_tx_hashes(self, tx_hashes: List[Union[str, bytes]]) -> List['EthereumTx']:
        # Search first in database
        ethereum_txs_dict = OrderedDict.fromkeys([HexBytes(tx_hash).hex() for tx_hash in tx_hashes])
        db_ethereum_txs = self.filter(tx_hash__in=tx_hashes).exclude(block=None)
        for db_ethereum_tx in db_ethereum_txs:
            ethereum_txs_dict[db_ethereum_tx.tx_hash] = db_ethereum_tx

        # Retrieve from the node the txs missing from database
        tx_hashes_not_in_db = [tx_hash for tx_hash, ethereum_tx in ethereum_txs_dict.items() if not ethereum_tx]
        if not tx_hashes_not_in_db:
            return list(ethereum_txs_dict.values())

        ethereum_client = EthereumClientProvider()

        tx_receipts = []
        for tx_hash, tx_receipt in zip(tx_hashes_not_in_db,
                                       ethereum_client.get_transaction_receipts(tx_hashes_not_in_db)):
            tx_receipt = tx_receipt or ethereum_client.get_transaction_receipt(tx_hash)  # Retry fetching if failed
            if not tx_receipt:
                raise TransactionNotFoundException(f'Cannot find tx-receipt with tx-hash={HexBytes(tx_hash).hex()}')
            elif tx_receipt.get('blockNumber') is None:
                raise TransactionWithoutBlockException(f'Cannot find blockNumber for tx-receipt with '
                                                       f'tx-hash={HexBytes(tx_hash).hex()}')
            else:
                tx_receipts.append(tx_receipt)

        txs = ethereum_client.get_transactions(tx_hashes_not_in_db)
        block_numbers = []
        for tx_hash, tx in zip(tx_hashes_not_in_db, txs):
            tx = tx or ethereum_client.get_transaction(tx_hash)  # Retry fetching if failed
            if not tx:
                raise TransactionNotFoundException(f'Cannot find tx with tx-hash={HexBytes(tx_hash).hex()}')
            elif tx.get('blockNumber') is None:
                raise TransactionWithoutBlockException(f'Cannot find blockNumber for tx with '
                                                       f'tx-hash={HexBytes(tx_hash).hex()}')
            block_numbers.append(tx['blockNumber'])

        blocks = ethereum_client.get_blocks(block_numbers)

        current_block_number = ethereum_client.current_block_number
        for tx, tx_receipt, block in zip(txs, tx_receipts, blocks):
            ethereum_block = EthereumBlock.objects.get_or_create_from_block(block,
                                                                            current_block_number=current_block_number)
            try:
                ethereum_tx = self.get(tx_hash=tx['hash'])
                # For txs stored before being mined
                ethereum_tx = self.__update_with_receipt_and_block(ethereum_tx, ethereum_block, tx_receipt)
                ethereum_txs_dict[HexBytes(ethereum_tx.tx_hash).hex()] = ethereum_tx
            except self.model.DoesNotExist:
                ethereum_tx = self.create_from_tx_dict(tx, tx_receipt=tx_receipt, ethereum_block=ethereum_block)
                ethereum_txs_dict[HexBytes(ethereum_tx.tx_hash).hex()] = ethereum_tx
        return list(ethereum_txs_dict.values())

    def create_or_update_from_tx_hash(self, tx_hash: str) -> 'EthereumTx':
        ethereum_client = EthereumClientProvider()
        try:
            ethereum_tx = self.get(tx_hash=tx_hash)
            # For txs stored before being mined
            if ethereum_tx.block is None:
                tx_receipt = ethereum_client.get_transaction_receipt(tx_hash)
                ethereum_block = EthereumBlock.objects.get_or_create_from_block_number(tx_receipt['blockNumber'])
                ethereum_tx = self.__update_with_receipt_and_block(ethereum_tx, ethereum_block, tx_receipt)
            return ethereum_tx
        except self.model.DoesNotExist:
            tx_receipt = ethereum_client.get_transaction_receipt(tx_hash)
            ethereum_block = EthereumBlock.objects.get_or_create_from_block_number(tx_receipt['blockNumber'])
            tx = ethereum_client.get_transaction(tx_hash)
            return self.create_from_tx_dict(tx, tx_receipt=tx_receipt, ethereum_block=ethereum_block)

    def create_from_tx_dict(self, tx: Dict[str, Any], tx_receipt: Optional[Dict[str, Any]] = None,
                            ethereum_block: Optional[EthereumBlock] = None) -> 'EthereumTx':
        return super().create(
            block=ethereum_block,
            tx_hash=HexBytes(tx['hash']).hex(),
            _from=tx['from'],
            gas=tx['gas'],
            gas_price=tx['gasPrice'],
            gas_used=tx_receipt and tx_receipt['gasUsed'],
            logs=tx_receipt and [clean_receipt_log(log) for log in tx_receipt.get('logs', list())],
            status=tx_receipt and tx_receipt.get('status'),
            transaction_index=tx_receipt and tx_receipt['transactionIndex'],
            data=HexBytes(tx.get('data') or tx.get('input')),
            nonce=tx['nonce'],
            to=tx.get('to'),
            value=tx['value'],
        )


class EthereumTx(TimeStampedModel):
    objects = EthereumTxManager()
    block = models.ForeignKey(EthereumBlock, on_delete=models.CASCADE, null=True, default=None,
                              related_name='txs')  # If mined
    tx_hash = Sha3HashField(unique=True, primary_key=True)
    gas_used = Uint256Field(null=True, default=None)  # If mined
    status = models.IntegerField(null=True, default=None)  # If mined. Old txs don't have `status`
    logs = ArrayField(JSONField(), null=True, default=None)  # If mined
    transaction_index = models.PositiveIntegerField(null=True, default=None)  # If mined
    _from = EthereumAddressField(null=True, db_index=True)
    gas = Uint256Field()
    gas_price = Uint256Field()
    data = models.BinaryField(null=True)
    nonce = Uint256Field()
    to = EthereumAddressField(null=True, db_index=True)
    value = Uint256Field()

    def __str__(self):
        return '{} from={} to={}'.format(self.tx_hash, self._from, self.to)

    @property
    def success(self) -> Optional[bool]:
        if self.status is not None:
            return self.status == 1


class EthereumEventQuerySet(models.QuerySet):
    def not_erc_20_721_events(self):
        return self.exclude(topic=ERC20_721_TRANSFER_TOPIC)

    def erc20_and_721_events(self, token_address: Optional[str] = None, address: Optional[str] = None):
        queryset = self.filter(topic=ERC20_721_TRANSFER_TOPIC)
        if token_address:
            queryset = queryset.filter(address=token_address)
        if address:
            queryset = queryset.filter(Q(arguments__to=address) | Q(arguments__from=address))
        return queryset

    def erc20_events(self, token_address: Optional[str] = None, address: Optional[str] = None):
        return self.erc20_and_721_events(token_address=token_address,
                                         address=address).filter(arguments__has_key='value')

    def erc721_events(self, token_address: Optional[str] = None, address: Optional[str] = None):
        return self.erc20_and_721_events(token_address=token_address,
                                         address=address).filter(arguments__has_key='tokenId')


class EthereumEventManager(BulkCreateSignalMixin, models.Manager):
    def from_decoded_event(self, decoded_event: Dict[str, Any]) -> 'EthereumEvent':
        """
        Does not create the model. Requires that `ethereum_tx` exists
        :param decoded_event:
        :return: `EthereumEvent` instance (not stored in database)
        """
        return EthereumEvent(ethereum_tx_id=decoded_event['transactionHash'],
                             log_index=decoded_event['logIndex'],
                             address=decoded_event['address'],
                             topic=decoded_event['topics'][0],
                             topics=decoded_event['topics'],
                             arguments=decoded_event['args'])

    def erc20_tokens_used_by_address(self, address: str) -> List[str]:
        """
        :param address:
        :return: List of token addresses used by an address
        """
        return self.erc20_events(address=address).values_list('address', flat=True).distinct()

    def erc20_tokens_with_balance(self, address: str) -> List[Dict[str, Any]]:
        """
        :return: List of dictionaries {'token_address': str, 'balance': int}
        """
        arguments_value_field = RawSQL("(arguments->>'value')::numeric", ())
        return self.erc20_events(
            address=address
        ).values('address').annotate(
            balance=Sum(Case(
                When(arguments__from=address, then=-arguments_value_field),
                default=arguments_value_field,
            ))
        ).order_by('-balance').values('address', 'balance')

    def get_or_create_erc20_or_721_event(self, decoded_event: Dict[str, Any]):
        if 'value' not in decoded_event['args'] or 'tokenId' not in decoded_event['args']:
            raise ValueError('Invalid ERC20 or ERC721 event %s' % decoded_event)
        else:
            return self.get_or_create(ethereum_tx_id=decoded_event['transactionHash'],
                                      log_index=decoded_event['logIndex'],
                                      defaults={
                                          'address': decoded_event['address'],
                                          'topic': decoded_event['topics'][0],
                                          'topics': decoded_event['topics'],
                                          'arguments': decoded_event['args'],
                                      })


class EthereumEvent(models.Model):
    objects = EthereumEventManager.from_queryset(EthereumEventQuerySet)()
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='events')
    log_index = models.PositiveIntegerField()
    address = EthereumAddressField(db_index=True)
    topic = Sha3HashField(db_index=True)
    topics = ArrayField(Sha3HashField())
    arguments = JSONField()

    class Meta:
        unique_together = (('ethereum_tx', 'log_index'),)

    def __str__(self):
        return f'Tx-hash={self.ethereum_tx_id} Log-index={self.log_index} Topic={self.topic} Arguments={self.arguments}'

    def is_erc20(self) -> bool:
        return self.topic == ERC20_721_TRANSFER_TOPIC and 'value' in self.arguments

    def is_erc721(self) -> bool:
        return self.topic == ERC20_721_TRANSFER_TOPIC and 'tokenId' in self.arguments


class InternalTxManager(BulkCreateSignalMixin, models.Manager):
    def _trace_address_to_str(self, trace_address) -> str:
        return ','.join([str(address) for address in trace_address])

    def build_from_trace(self, trace: Dict[str, Any], ethereum_tx: EthereumTx) -> 'InternalTx':
        tx_type = EthereumTxType.parse(trace['type'])
        call_type = EthereumTxCallType.parse_call_type(trace['action'].get('callType'))
        trace_address_str = self._trace_address_to_str(trace['traceAddress'])
        return InternalTx(
            ethereum_tx=ethereum_tx,
            trace_address=trace_address_str,
            _from=trace['action'].get('from'),
            gas=trace['action'].get('gas', 0),
            data=trace['action'].get('input') or trace['action'].get('init'),
            to=trace['action'].get('to') or trace['action'].get('address'),
            value=trace['action'].get('value') or trace['action'].get('balance', 0),
            gas_used=(trace.get('result') or {}).get('gasUsed', 0),
            contract_address=(trace.get('result') or {}).get('address'),
            code=(trace.get('result') or {}).get('code'),
            output=(trace.get('result') or {}).get('output'),
            refund_address=trace['action'].get('refundAddress'),
            tx_type=tx_type.value,
            call_type=call_type.value if call_type else None,
            error=trace.get('error')
        )

    def get_or_create_from_trace(self, trace: Dict[str, Any], ethereum_tx: EthereumTx) -> Tuple['InternalTx', bool]:
        tx_type = EthereumTxType.parse(trace['type'])
        call_type = EthereumTxCallType.parse_call_type(trace['action'].get('callType'))
        trace_address_str = self._trace_address_to_str(trace['traceAddress'])
        return self.get_or_create(
            ethereum_tx=ethereum_tx,
            trace_address=trace_address_str,
            defaults={
                '_from': trace['action'].get('from'),
                'gas': trace['action'].get('gas', 0),
                'data': trace['action'].get('input') or trace['action'].get('init'),
                'to': trace['action'].get('to') or trace['action'].get('address'),
                'value': trace['action'].get('value') or trace['action'].get('balance', 0),
                'gas_used': (trace.get('result') or {}).get('gasUsed', 0),
                'contract_address': (trace.get('result') or {}).get('address'),
                'code': (trace.get('result') or {}).get('code'),
                'output': (trace.get('result') or {}).get('output'),
                'refund_address': trace['action'].get('refundAddress'),
                'tx_type': tx_type.value,
                'call_type': call_type.value if call_type else None,
                'error': trace.get('error'),
            }
        )


class InternalTxQuerySet(models.QuerySet):
    def incoming_txs(self, address: str):
        return self.filter(to=address,
                           call_type=EthereumTxCallType.CALL.value,
                           value__gt=0
                           ).annotate(
            transaction_hash=F('ethereum_tx_id'),
            block_number=F('ethereum_tx__block_id'),
            execution_date=F('ethereum_tx__block__timestamp'),
            token_address=Value(None, output_field=EthereumAddressField())
        ).order_by('-ethereum_tx__block_id')

    def incoming_tokens(self, address: str):
        return EthereumEvent.objects.erc20_events().filter(
            arguments__to=address
        ).annotate(
            to=RawSQL("arguments->>%s", ('to',)),  # Order is really important!
            _from=RawSQL("arguments->>%s", ('from',)),
            value=RawSQL("(arguments->>%s)::numeric", ('value',)),
            transaction_hash=F('ethereum_tx_id'),
            block_number=F('ethereum_tx__block_id'),
            execution_date=F('ethereum_tx__block__timestamp'),
            token_address=F('address')
        ).order_by('-ethereum_tx__block_id')

    def incoming_txs_with_tokens(self, address: str):
        tokens_queryset = self.incoming_tokens(address)
        ether_queryset = self.incoming_txs(address)
        return self.union_incoming_txs_with_tokens(tokens_queryset, ether_queryset)

    def union_incoming_txs_with_tokens(self, tokens_queryset, ether_queryset):
        values = ('block_number', 'transaction_hash', 'to', '_from', 'value', 'execution_date', 'token_address')
        return ether_queryset.values(*values).union(tokens_queryset.values(*values)).order_by('-block_number')

    def can_be_decoded(self):
        return self.filter(
            call_type=EthereumTxCallType.DELEGATE_CALL.value,
            error=None,
            ethereum_tx__status=1,
            decoded_tx=None,
        ).exclude(data=None)


class InternalTx(models.Model):
    objects = InternalTxManager.from_queryset(InternalTxQuerySet)()
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='internal_txs')
    _from = EthereumAddressField(null=True, db_index=True)  # For SELF-DESTRUCT it can be null
    gas = Uint256Field()
    data = models.BinaryField(null=True)  # `input` for Call, `init` for Create
    to = EthereumAddressField(null=True, db_index=True)
    value = Uint256Field()
    gas_used = Uint256Field()
    contract_address = EthereumAddressField(null=True, db_index=True)  # Create
    code = models.BinaryField(null=True)                # Create
    output = models.BinaryField(null=True)              # Call
    refund_address = EthereumAddressField(null=True, db_index=True)  # For SELF-DESTRUCT
    tx_type = models.PositiveSmallIntegerField(choices=[(tag.value, tag.name) for tag in EthereumTxType], db_index=True)
    call_type = models.PositiveSmallIntegerField(null=True,
                                                 choices=[(tag.value, tag.name) for tag in EthereumTxCallType],
                                                 db_index=True)  # Call
    trace_address = models.CharField(max_length=600)  # Stringified traceAddress
    error = models.CharField(max_length=200, null=True)

    class Meta:
        unique_together = (('ethereum_tx', 'trace_address'),)

    def __str__(self):
        if self.to:
            return 'Internal tx hash={} from={} to={}'.format(self.ethereum_tx_id, self._from, self.to)
        else:
            return 'Internal tx hash={} from={}'.format(self.ethereum_tx_id, self._from)

    @property
    def block_number(self):
        return self.ethereum_tx.block_id

    @property
    def can_be_decoded(self):
        return (self.is_delegate_call
                and not self.error
                and self.data
                and self.ethereum_tx.success)

    @property
    def is_call(self):
        return EthereumTxType(self.tx_type) == EthereumTxType.CALL

    @property
    def is_decoded(self):
        try:
            return bool(self.decoded_tx)
        except InternalTxDecoded.DoesNotExist:
            return False

    @property
    def is_delegate_call(self) -> bool:
        if self.call_type is None:
            return False
        else:
            return EthereumTxCallType(self.call_type) == EthereumTxCallType.DELEGATE_CALL

    @property
    def is_ether_transfer(self) -> bool:
        return self.call_type == EthereumTxCallType.CALL.value and self.value > 0

    def get_next_trace(self) -> Optional['InternalTx']:
        internal_txs = InternalTx.objects.filter(ethereum_tx=self.ethereum_tx).order_by('trace_address')
        traces = [it.trace_address for it in internal_txs]
        index = traces.index(self.trace_address)
        try:
            return internal_txs[index + 1]
        except IndexError:
            return None

    def get_previous_trace(self) -> Optional['InternalTx']:
        internal_txs = InternalTx.objects.filter(ethereum_tx=self.ethereum_tx).order_by('trace_address')
        traces = [it.trace_address for it in internal_txs]
        index = traces.index(self.trace_address)
        try:
            return internal_txs[index - 1]
        except IndexError:
            return None


class InternalTxDecodedManager(models.Manager):
    pass


class InternalTxDecodedQuerySet(models.QuerySet):
    def not_processed(self):
        return self.filter(processed=False)

    def pending_for_safes(self):
        """
        :return: Pending `InternalTxDecoded` sorted by block number and then transaction index inside the block
        """
        return self.not_processed(
        ).filter(
            Q(internal_tx___from__in=SafeContract.objects.values('address'))  # Just Safes indexed
            | Q(function_name='setup')  # This way we can index new Safes without events
        ).select_related(
            'internal_tx',
            'internal_tx__ethereum_tx',
        ).order_by(
            'internal_tx__ethereum_tx__block_id',
            'internal_tx__ethereum_tx__transaction_index',
            'internal_tx__trace_address',
        )


class InternalTxDecoded(models.Model):
    objects = InternalTxDecodedManager.from_queryset(InternalTxDecodedQuerySet)()
    internal_tx = models.OneToOneField(InternalTx, on_delete=models.CASCADE, related_name='decoded_tx',
                                       primary_key=True)
    function_name = models.CharField(max_length=256)
    arguments = JSONField()
    processed = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "Internal txs decoded"

    @property
    def address(self) -> str:
        return self.internal_tx._from

    @property
    def block_number(self) -> int:
        return self.internal_tx.ethereum_tx.block_id

    @property
    def tx_hash(self) -> str:
        return self.internal_tx.ethereum_tx_id

    def set_processed(self):
        self.processed = True
        return self.save(update_fields=['processed'])


class MultisigTransactionQuerySet(models.QuerySet):
    def executed(self):
        return self.exclude(
            ethereum_tx__block=None
        )

    def not_executed(self):
        return self.filter(
            ethereum_tx__block=None
        )

    def with_confirmations_required(self):
        threshold_query = SafeStatus.objects.filter(
            internal_tx__ethereum_tx=OuterRef('ethereum_tx')
        ).sorted_reverse_by_internal_tx().values('threshold')

        return self.annotate(confirmations_required=Subquery(threshold_query[:1]))


class MultisigTransaction(TimeStampedModel):
    objects = MultisigTransactionQuerySet.as_manager()
    safe_tx_hash = Sha3HashField(primary_key=True)
    safe = EthereumAddressField(db_index=True)
    ethereum_tx = models.ForeignKey(EthereumTx, null=True, default=None, blank=True,
                                    on_delete=models.SET_NULL, related_name='multisig_txs')
    to = EthereumAddressField(null=True, db_index=True)
    value = Uint256Field()
    data = models.BinaryField(null=True)
    operation = models.PositiveSmallIntegerField(choices=[(tag.value, tag.name) for tag in SafeOperation])
    safe_tx_gas = Uint256Field()
    base_gas = Uint256Field()
    gas_price = Uint256Field()
    gas_token = EthereumAddressField(null=True)
    refund_receiver = EthereumAddressField(null=True)
    signatures = models.BinaryField(null=True)  # When tx is executed
    nonce = Uint256Field(db_index=True)
    failed = models.NullBooleanField(default=None)

    def __str__(self):
        return f'{self.safe} - {self.nonce} - {self.safe_tx_hash}'

    @property
    def execution_date(self) -> Optional[datetime.datetime]:
        if self.ethereum_tx_id and self.ethereum_tx.block:
            return self.ethereum_tx.block.timestamp
        return None

    @property
    def executed(self) -> bool:
        return bool(self.ethereum_tx_id and (self.ethereum_tx.block_id is not None))

    def owners(self) -> Optional[List[str]]:
        if not self.signatures:
            return None
        else:
            # TODO Get owners from signatures. Not very trivial
            return []


class MultisigConfirmationQuerySet(models.QuerySet):
    def without_transaction(self):
        return self.filter(multisig_transaction=None)

    def with_transaction(self):
        return self.exclude(multisig_transaction=None)


class MultisigConfirmation(TimeStampedModel):
    objects = MultisigConfirmationQuerySet.as_manager()
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='multisig_confirmations',
                                    null=True)  # `null=True` for signature confirmations
    multisig_transaction = models.ForeignKey(MultisigTransaction,
                                             on_delete=models.CASCADE,
                                             null=True,
                                             related_name="confirmations")
    multisig_transaction_hash = Sha3HashField(null=True,
                                              db_index=True)  # Use this while we don't have a `multisig_transaction`
    owner = EthereumAddressField()

    signature = HexField(null=True, default=None, max_length=500)  # Off chain signatures

    class Meta:
        unique_together = (('multisig_transaction_hash', 'owner'),)

    def __str__(self):
        if self.multisig_transaction_id:
            return f'Confirmation of owner={self.owner} for transaction-hash={self.multisig_transaction_hash}'
        else:
            return f'Confirmation of owner={self.owner} for existing transaction={self.multisig_transaction_hash}'


class MonitoredAddressManager(models.Manager):
    def update_addresses(self, addresses: List[str], from_block_number: int, block_number: int,
                         database_field: str) -> int:
        """
        :param addresses: Addresses to have the block number updated
        :param from_block_number: Make sure that no reorg has happened checking that block number was not rollbacked
        :param block_number: Block number to be updated
        :param database_field: Database field to store the block number
        :return: Number of entities updated
        """
        return self.filter(
            **{'address__in': addresses,
               database_field + '__gte': from_block_number - 1,  # Protect in case of reorg
               }
        ).update(**{database_field: block_number})


class MonitoredAddressQuerySet(models.QuerySet):
    def almost_updated(self, database_field: str, current_block_number: int,
                       updated_blocks_behind: int, confirmations: int):
        return self.filter(
            **{database_field + '__lt': current_block_number - confirmations,
               database_field + '__gt': current_block_number - updated_blocks_behind})

    def not_updated(self, database_field: str, current_block_number: int, confirmations: int):
        return self.filter(
            **{database_field + '__lt': current_block_number - confirmations}
        )


class MonitoredAddress(models.Model):
    objects = MonitoredAddressManager.from_queryset(MonitoredAddressQuerySet)()
    address = EthereumAddressField(primary_key=True)
    initial_block_number = models.IntegerField(default=0)  # Block number when address received first tx
    tx_block_number = models.IntegerField(null=True, default=None)  # Block number when last internal tx scan ended

    class Meta:
        abstract = True
        verbose_name_plural = "Monitored addresses"

    def __str__(self):
        return f'Address={self.address} - Initial-block-number={self.initial_block_number}' \
               f' - Tx-block-number={self.tx_block_number}'


class ProxyFactory(MonitoredAddress):
    class Meta:
        verbose_name_plural = "Proxy factories"
        ordering = ['tx_block_number']


class SafeMasterCopy(MonitoredAddress):
    class Meta:
        verbose_name_plural = "Safe master copies"
        ordering = ['tx_block_number']


class SafeContractManager(MonitoredAddressManager):
    pass


class SafeContract(models.Model):
    objects = SafeContractManager.from_queryset(MonitoredAddressQuerySet)()
    address = EthereumAddressField(primary_key=True)
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='safe_contracts')
    erc20_block_number = models.IntegerField(default=0)  # Block number of last scan of erc20

    def __str__(self):
        return f'Safe address={self.address} - ethereum-tx={self.ethereum_tx_id}'

    @property
    def created_block_number(self) -> Optional[int]:
        if self.ethereum_tx:
            return self.ethereum_tx.block_id


class SafeStatusManager(models.Manager):
    pass


class SafeStatusQuerySet(models.QuerySet):
    def sorted_by_internal_tx(self):
        return self.order_by(
            'address',
            '-internal_tx__ethereum_tx__block_id',
            '-internal_tx__ethereum_tx__transaction_index',
            '-internal_tx__trace_address',
        )

    def sorted_reverse_by_internal_tx(self):
        return self.order_by(
            'address',
            'internal_tx__ethereum_tx__block_id',
            'internal_tx__ethereum_tx__transaction_index',
            'internal_tx__trace_address',
        )

    def addresses_for_owner(self, owner_address: str) -> List[str]:
        return self.filter(
            owners__contains=[owner_address],
            internal_tx__in=self.last_for_every_address().values('pk')
        ).values_list('address', flat=True)

    def last_for_every_address(self) -> QuerySet:
        return self.distinct(
            'address'  # Uses PostgreSQL `DISTINCT ON`
        ).select_related(
            'internal_tx__ethereum_tx'
        ).sorted_by_internal_tx()

    def last_for_address(self, address: str) -> Optional['SafeStatus']:
        safe_status = self.last_for_every_address().filter(
            address=address
        ).first()
        if not safe_status:
            logger.error('SafeStatus not found for address=%s', address)
        return safe_status


class SafeStatus(models.Model):
    objects = SafeStatusManager.from_queryset(SafeStatusQuerySet)()
    internal_tx = models.OneToOneField(InternalTx, on_delete=models.CASCADE, related_name='safe_status',
                                       primary_key=True)
    address = EthereumAddressField(db_index=True)
    owners = ArrayField(EthereumAddressField())
    threshold = Uint256Field()
    nonce = Uint256Field(default=0)
    master_copy = EthereumAddressField()

    class Meta:
        unique_together = (('internal_tx', 'address'),)
        verbose_name_plural = 'Safe statuses'

    def __str__(self):
        return f'safe={self.address} threshold={self.threshold} owners={self.owners} nonce={self.nonce}'

    @property
    def block_number(self):
        return self.internal_tx.ethereum_tx.block_id

    def store_new(self, internal_tx: InternalTx) -> None:
        self.internal_tx = internal_tx
        return self.save()


class WebHookType(Enum):
    NEW_CONFIRMATION = 0
    PENDING_MULTISIG_TRANSACTION = 1
    EXECUTED_MULTISIG_TRANSACTION = 2
    INCOMING_ETHER = 3
    INCOMING_TOKEN = 4


class WebHook(models.Model):
    address = EthereumAddressField(db_index=True)
    url = models.URLField()
    # Configurable webhook types to listen to
    new_confirmation = models.BooleanField(default=True)
    pending_outgoing_transaction = models.BooleanField(default=True)
    new_executed_outgoing_transaction = models.BooleanField(default=True)
    new_incoming_transaction = models.BooleanField(default=True)

    class Meta:
        unique_together = (('address', 'url'),)

    def __str__(self):
        return f'Webhook for safe={self.address} to url={self.url}'
