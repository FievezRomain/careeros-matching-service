from typing import Protocol


class MigrationRunner(Protocol):
    def apply_pending(self) -> None: ...
    def verify(self) -> bool: ...


class StartRecommendations:
    def __init__(self, runner: MigrationRunner) -> None:
        self._runner = runner

    def execute(self) -> None:
        self._runner.apply_pending()
        if not self._runner.verify():
            raise RuntimeError("recommendation schema is not at its expected head")
