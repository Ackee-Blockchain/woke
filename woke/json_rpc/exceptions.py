class JsonRpcError(Exception):
    __code: int
    __message: str

    def __init__(self, code: int, message: str):
        self.__code = code
        self.__message = message

    @property
    def code(self) -> int:
        return self.__code

    @property
    def message(self) -> str:
        return self.__message
