BROKER_FAILURE_STAGES = frozenset(
    {
        "token",
        "upload-discovery",
        "upload-rpc",
        "receive-discovery",
        "receive-rpc",
        "pr-request",
        "issue-request",
        "response-stream",
    }
)


class BrokerStageError(RuntimeError):
    def __init__(self, stage: str) -> None:
        if stage not in BROKER_FAILURE_STAGES:
            raise ValueError("GitHub broker failure stage is invalid")
        self.stage = stage
        super().__init__("GitHub broker operation failed")
