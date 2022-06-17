import aiohttp
from aiohttp import ClientSession

from woke.cli.console import console

from .data_model import *


class JsonRpcCommunicator:
    __client_session: Optional[ClientSession]
    __port: int
    __request_id: int
    __url: str

    def __init__(
        self, port: int = 8545, client_session: Optional[ClientSession] = None
    ):
        self.__client_session = client_session
        self.__port = port
        self.__request_id = 0
        self.__url = f"http://localhost:{port}"

    async def __send_request(
        self, method_name: str, params: Optional[List] = None
    ) -> str:
        request_data = JsonRpcRequest(
            method=method_name,
            params=(params if params is not None else []),
            id=self.__request_id,
        )
        post_data = request_data.json()
        self.__request_id += 1

        if self.__client_session is not None:
            async with self.__client_session.post(
                self.__url, data=post_data
            ) as response:
                text = await response.text()
                # console.print_json(text)  # TODO
                return text
        else:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.__url, data=post_data) as response:
                    text = await response.text()
                    console.print_json(text)  # TODO
                    return text

    async def eth_block_number(self) -> int:
        """Returns the number of most recent block."""
        text = await self.__send_request("eth_blockNumber")
        response = JsonRpcResponseEthBlockNumber.parse_raw(text)
        return response.result

    async def eth_get_block_by_number(
        self, block_number: int, full_transactions: bool
    ) -> JsonRpcBlock:
        """Returns information about a block by number."""
        text = await self.__send_request(
            "eth_getBlockByNumber", [hex(block_number), full_transactions]
        )
        response = JsonRpcReponseEthGetBlock.parse_raw(text)
        return response.result

    async def eth_get_transaction_receipt(self, transaction_hash: str):
        """Returns the receipt of a transaction by transaction hash."""
        text = await self.__send_request(
            "eth_getTransactionReceipt", [transaction_hash]
        )
        response = JsonRpcResponseEthGetTransactionReceipt.parse_raw(text)
        return response.result

    async def eth_get_code(self, address: str, block_number: Union[int, str]) -> str:
        """Returns code at a given address."""
        text = await self.__send_request(
            "eth_getCode",
            [
                address,
                hex(block_number) if isinstance(block_number, int) else block_number,
            ],
        )
        response = JsonRpcResponseEthGetCode.parse_raw(text)
        return response.result

    async def eth_get_storage_at(
        self, address: str, storage_slot: int, block_number: Union[int, str]
    ):
        """Returns the value from a storage position at a given address."""
        text = await self.__send_request(
            "eth_getStorageAt",
            [
                address,
                hex(storage_slot),
                hex(block_number) if isinstance(block_number, int) else block_number,
            ],
        )
        response = JsonRpcResponseEthGetStorageAt.parse_raw(text)
        return response.result
