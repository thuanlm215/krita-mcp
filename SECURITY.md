# Security

The plugin listens on loopback only. Each Krita session writes a random token into Krita's data folder, and the server sends that token with every request.

Do not put `KRITA_MCP_TOKEN`, discovery files, or Krita documents into issues, pull requests, or commits. A trace log can contain document paths. Trim it before sharing.

`close_document` can still take Krita down on some teardown paths. That is a stability bug, not an access boundary.

Report a vulnerability through a private GitHub security advisory on this repository. Give the Krita version, the operating system, and what the bridge did. Do not include the session token.
