from typing import List, Optional, Union

from pydantic import BaseModel, Extra, Field, validator

from .exceptions import JsonRpcError


def _to_camel(s: str) -> str:
    split = s.split("_")
    return split[0].lower() + "".join([w.capitalize() for w in split[1:]])


class JsonRpcModel(BaseModel):
    class Config:
        alias_generator = _to_camel
        allow_mutation = False
        extra = Extra.forbid


class JsonRpcRequest(JsonRpcModel):
    jsonrpc: str = "2.0"
    method: str
    params: List = []
    id: int


class JsonRpcResponseError(JsonRpcModel):
    code: int
    message: str
    data: Optional[str]


class JsonRpcResponse(JsonRpcModel):
    jsonrpc: str = "2.0"
    id: int
    error: Optional[JsonRpcResponseError]

    @validator("error", pre=True)
    def raise_on_error(cls, v):
        raise JsonRpcError(v["code"], v["message"])


class JsonRpcTransaction(JsonRpcModel):
    hash: str
    nonce: str
    block_hash: str
    block_number: str
    transaction_index: str
    from_addr: str = Field(alias="from")
    to_addr: Optional[str] = Field(alias="to")
    value: str
    gas: str
    gas_price: str
    input: str
    v: str
    r: str
    s: str
    type: Union[int, str]


class JsonRpcTransactionReceipt(JsonRpcModel):
    transaction_hash: str
    transaction_index: str
    block_hash: str
    block_number: str
    from_addr: str = Field(alias="from")
    to_addr: Optional[str] = Field(alias="to")
    gas_used: str
    cumulative_gas_used: str
    contract_address: str
    logs: List
    status: str
    logs_bloom: str
    effective_gas_price: str


class JsonRpcBlock(JsonRpcModel):
    number: str
    hash: str
    parent_hash: str
    nonce: str
    mix_hash: str
    sha3_uncles: str
    logs_bloom: str
    transactions_root: str
    state_root: str
    receipts_root: str
    miner: str
    difficulty: str
    total_difficulty: str
    extra_data: str
    size: str
    gas_limit: str
    gas_used: str
    timestamp: str
    transactions: List[JsonRpcTransaction]
    uncles: List
    base_fee_per_gas: Optional[str]


class JsonRpcResponseEthBlockNumber(JsonRpcResponse):
    result: int

    @validator("result", pre=True)
    def parse_result(cls, v):
        return int(v, 0)


class JsonRpcReponseEthGetBlock(JsonRpcResponse):
    result: JsonRpcBlock


class JsonRpcResponseEthGetTransactionReceipt(JsonRpcResponse):
    result: JsonRpcTransactionReceipt


class JsonRpcResponseEthGetCode(JsonRpcResponse):
    result: str


class JsonRpcResponseEthGetStorageAt(JsonRpcResponse):
    result: str
