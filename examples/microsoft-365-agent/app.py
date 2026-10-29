"""Microsoft 365 agent with Gate middleware and an explicitly guarded tool.

No model or Microsoft Graph credentials are needed for the deterministic status
tool. Microsoft channel authentication is configured through the Agents SDK.
"""
import asyncio
import os
from datetime import datetime, timezone

from aiohttp import web
from dotenv import load_dotenv
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_middleware, start_agent_process
from microsoft_agents.hosting.core import AgentApplication, MemoryStorage

import ggate


async def console():
    """Exercise real Microsoft middleware locally without Microsoft channel credentials."""
    from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount, ResourceResponse
    from microsoft_agents.hosting.core import TurnContext

    load_dotenv()
    if not os.getenv("GGATE_CONSOLE_URL") or not os.getenv("GGATE_API_KEY"):
        raise RuntimeError("Set GGATE_CONSOLE_URL and GGATE_API_KEY")
    gate = ggate.init(agent_name="Microsoft 365 Status Assistant", team=os.getenv("GGATE_TEAM", "Platform Engineering"))
    middleware = ggate.instrument("m365", sdk_client=gate)

    class ConsoleAdapter:
        async def send_activities(self, context, activities):
            for activity in activities:
                print(activity.text)
            return [ResourceResponse(id="local") for _ in activities]

    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "Service (empty to exit): ")
            except EOFError:
                break
            if not text:
                break
            activity = Activity(type="message", text=text, id=str(datetime.now(timezone.utc).timestamp()),
                                channel_id="console", service_url="http://localhost",
                                conversation=ConversationAccount(id="local-demo"),
                                from_property=ChannelAccount(id="local-user"), recipient=ChannelAccount(id="agent"))
            context = TurnContext(ConsoleAdapter(), activity)
            try:
                await middleware.on_turn(context, lambda ctx: respond(ctx, None, gate))
            except ggate.GgateBlockedError:
                print("Blocked by Gate. See Activity for the decision.")
    finally:
        await asyncio.to_thread(gate.flush)


async def respond(context, state, gate):
    """Replace this deterministic tool with your business operation after its preflight."""
    name = "get_service_status"
    arguments = {"service": context.activity.text or "demo"}
    await gate.scan_tool_call_async(name, input_summary=arguments, enforce=True)
    result = {"service": arguments["service"], "status": "healthy", "checked_at": datetime.now(timezone.utc).isoformat()}
    text = f"{result['service']}: {result['status']} (demo data, {result['checked_at']})"
    await gate.scan_tool_result_async(name, text, enforce=True)
    # Middleware checks this response before the Microsoft adapter delivers it.
    await context.send_activity(text)


def create_app():
    load_dotenv()
    if not os.getenv("GGATE_CONSOLE_URL") or not os.getenv("GGATE_API_KEY"):
        raise RuntimeError("Set GGATE_CONSOLE_URL and GGATE_API_KEY before starting this example")
    gate = ggate.init(agent_name="Microsoft 365 Status Assistant", team=os.getenv("GGATE_TEAM", "Platform Engineering"), mode="sync")
    configuration = load_configuration_from_env(os.environ)
    connections = MsalConnectionManager(**configuration)
    auth_configuration = connections.get_default_connection_configuration()
    if not auth_configuration.CLIENT_ID or not auth_configuration.CLIENT_ID.strip():
        raise RuntimeError("Set the Microsoft agent client ID for HTTP hosting, or use --console for a local demo")
    auth_configuration.ANONYMOUS_ALLOWED = False
    auth_configuration.VALIDATE_ISSUER = True
    adapter = CloudAdapter(connection_manager=connections)
    ggate.instrument("microsoft-365-agents", adapter=adapter, sdk_client=gate)
    agent = AgentApplication(connection_manager=connections, storage=MemoryStorage(), **configuration)

    @agent.activity("message")
    async def message(context, state):
        await respond(context, state, gate)

    async def on_error(context, error):
        # Do not echo blocked content or exception details into the channel.
        if not isinstance(error, ggate.GgateBlockedError):
            raise error
        # The turn is stopped; inspect the audited decision in Gate Activity.

    adapter.on_turn_error = on_error

    async def messages(request):
        return await start_agent_process(request, agent, adapter)

    async def health(request):
        return web.json_response({"status": "ok"})

    async def cleanup(app):
        await asyncio.to_thread(gate.flush)

    @web.middleware
    async def authenticate(request, handler):
        if request.path == "/health":
            return await handler(request)
        return await jwt_authorization_middleware(request, handler)

    app = web.Application(middlewares=[authenticate])
    app["agent_configuration"] = auth_configuration
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/health", health)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    import sys
    if "--console" in sys.argv:
        asyncio.run(console())
    else:
        web.run_app(create_app(), host="127.0.0.1", port=int(os.getenv("PORT", "3978")))
