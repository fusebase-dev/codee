"""Other agents talk to Codee: an MCP server at ``/mcp`` on the admin UI.

It is a resource server and its own OAuth authorization server. Who may approve
a client is decided by the sign-in proxy in front of the admin UI, which is the
only thing that knows who the user is. See ``oauth.py`` and ``server.py``.
"""
from codee_plugin_abstract.plugin import (
    AbstractCodeePlugin, PluginAction, PluginField)

from codee_mcp.store import Store

DEFAULT_USER_HEADER = "X-Forwarded-Email"


class McpPlugin(AbstractCodeePlugin):
    NAME = "mcp"
    DISPLAY_NAME = "MCP server"
    # Being on lets the clients people approve run agents on this host.
    ENABLED_BY_DEFAULT = False
    SETTINGS_FIELDS = [
        PluginField("user_header", "User header",
                    hint="Request header in which the sign-in proxy names the "
                         "signed-in user. The proxy must overwrite whatever "
                         "the browser sent in it.",
                    placeholder=DEFAULT_USER_HEADER,
                    default=DEFAULT_USER_HEADER),
    ]
    ACTIONS = [PluginAction("revoke_all", "Revoke all access")]
    DESCRIPTION = """\
Off until you turn it on. MCP server URL:

```
{ADMIN_URL}/mcp
```

Add it to an MCP client, for example:

```
claude mcp add --transport http codee {ADMIN_URL}/mcp
```

The client opens a browser: sign in as you do for this page and press **Allow**.
It gets one tool, `send_message`:

| Argument | | |
| --- | --- | --- |
| `message` | required | What to tell the agent. |
| `provider` | optional | `claude_code`, `github_copilot`, `codex` or `opencode`; the default agent from Settings if left out. |
| `model` | optional | A model id of that provider. |
| `session_id` | optional | Continues the conversation an earlier call returned. |

It returns `response` and `session_id`. A conversation can be continued only by
the user who started it. Agents run **with every permission on this host**, so
approve a client as you would hand out a shell.

**Behind a sign-in proxy**, let only these paths through without sign-in; they
authenticate themselves:

- `/mcp`
- `/mcp/oauth/token`, `/mcp/oauth/register`
- `/.well-known/oauth-protected-resource`, `/.well-known/oauth-protected-resource/mcp`
- `/.well-known/oauth-authorization-server`

Keep `/mcp/oauth/authorize` and `/mcp/oauth/consent` **behind** sign-in. Match
the paths exactly: a prefix rule on `/mcp` would open those two as well. With
Caddy in front of oauth2-proxy:

```
@mcp path /mcp /mcp/oauth/token /mcp/oauth/register /.well-known/oauth-protected-resource /.well-known/oauth-protected-resource/mcp /.well-known/oauth-authorization-server
handle @mcp {
	reverse_proxy 127.0.0.1:8501
}
handle {
	reverse_proxy 127.0.0.1:4181
}
```

Use this Codee's own port instead of 8501, and make sure that port can't be
reached except through the proxy.
"""

    def store(self) -> Store:
        return Store(self.data_dir / "mcp.db")

    def http_routes(self, host):
        # Imported here: the executor loads the plugins too and serves nothing.
        from codee_mcp.server import routes
        return routes(self.store(), host)

    def run_action(self, key, settings):
        if key != "revoke_all":
            return super().run_action(key, settings)
        count = self.store().revoke_all()
        return f"Revoked {count} client approval{'' if count == 1 else 's'}."
