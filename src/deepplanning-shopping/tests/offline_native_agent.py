"""Harbor test agent: original OpenAI client talks only to a local fixture."""

from pathlib import Path

from deepplanning_shopping.native_agent import NativeAgentOptions, ShoppingFnAgent


class OfflineOptions(NativeAgentOptions):
    responses_path: str


class OfflineShoppingFnAgent(ShoppingFnAgent):
    options_model = OfflineOptions

    async def setup(self, environment):
        await super().setup(environment)
        await environment.upload_dir(
            Path(__file__).parent / "fixtures/offline_native",
            "/workspace/offline_fixture",
        )
        await environment.upload_file(
            self.options.responses_path, "/workspace/offline_fixture/responses.json"
        )

    def execution_env(self):
        return {**super().execution_env(), "PYTHONPATH": "/workspace/offline_fixture"}

    async def run(self, instruction, environment, context):
        await super().run(instruction, environment, context)
        await environment.download_file(
            "/workspace/offline_fixture/requests.jsonl",
            self.logs_dir / "offline-requests.jsonl",
        )
        result = await environment.exec(
            "test ! -e /workspace/offline_fixture/blocked-network.txt"
        )
        if result.return_code:
            raise RuntimeError(
                "Offline fixture observed an attempted external connection"
            )
