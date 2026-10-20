"""Setting up a local model: `nox ai setup`, the optional onboarding step and the dashboard tile.

One flow behind all three: measure the machine, check Ollama, recommend a model from the curated
table in `nox.ai.local_models`, download it, probe it with a plain question and a tool call, and
only then write `ai.providers.ollama.model` to `user.yaml`. See `nox.localai.setup` for the flow,
`nox.localai.install` for the dashboard's requests.
"""
