from research_core.plugin import register


class FakeContext:
    def __init__(self):
        self.tools = []
        self.hooks = []
        self.sections = []
        self.middleware = []

    def register_middleware(self, name, callback):
        self.middleware.append((name, callback))

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_system_prompt_section(self, section_id, content, **kwargs):
        self.sections.append((section_id, content, kwargs))


def test_make_register_builds_a_callable_per_role():
    for role in ("orchestrator", "researcher"):
        ctx = FakeContext()
        register.make_register(role)(ctx)
        assert [name for name, _ in ctx.middleware] == [
            *["llm_request"] * 3,
            "tool_request",
            "tool_execution",
        ]
        for tool in ctx.tools:
            assert tool["toolset"] == f"ide-{role}"
            assert set(tool["schema"]) >= {"name", "description", "parameters"}
        assert {name for name, _ in ctx.hooks} >= {"transform_tool_result", "post_tool_call"}
        assert "ide-briefing" in {section_id for section_id, _, _ in ctx.sections}
        assert "on_session_end" in {name for name, _ in ctx.hooks}


def test_firmer_refusal_registers_before_the_advisory_note():
    # Hermes keeps the first string a transform_tool_result hook returns.
    ctx = FakeContext()
    register.make_register("researcher")(ctx)
    transforms = [cb for name, cb in ctx.hooks if name == "transform_tool_result"]
    assert [cb.__module__ for cb in transforms] == [
        "research_core.plugin.repeats",
        "research_core.plugin.advisory",
    ]
