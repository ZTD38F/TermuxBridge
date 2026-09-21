# TermuxBridge Security

## Trust boundary

TermuxBridge executes with the permissions of the Termux application/user on Android. It is **not** Android root unless the device is separately rooted and explicit root access is configured.

## Never commit secrets

The repository must never contain:

- OpenAI API keys or Secure MCP Tunnel control-plane/runtime keys
- OAuth access or refresh tokens
- cookies, session exports or browser authentication state
- Google account passwords, OTP codes or recovery material
- private tunnel URLs when they function as credentials
- authenticated Chromium profiles
- unredacted logs containing secrets

Secrets belong only in local protected storage with restrictive permissions.

## Network model

The MCP service should remain local/private. Existing deployments use loopback for the local MCP endpoint. Remote access should be provided through the secure tunnel layer rather than by exposing an unauthenticated command-capable HTTP service directly to the public Internet.

## Change discipline

For consequential changes:

1. inspect current runtime state;
2. preserve the working configuration or backup when appropriate;
3. make the smallest necessary change;
4. verify local MCP health;
5. verify tunnel connectivity separately;
6. perform an end-to-end tool call;
7. synchronize the confirmed working change back to GitHub.

A local edit that has not been synchronized back to GitHub is considered deployment drift.
