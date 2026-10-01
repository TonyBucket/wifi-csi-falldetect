# Workspace Instructions & Tool Rules

## Subagent Tool Calling Rule
- When calling the tool `subagent`:
  - **Leave `sessionID` undefined/omitted** when creating a new task or calling a subagent for the first time.
  - **NEVER** pass the current parent session ID into `sessionID`. A session is NOT its own child. Passing the parent session ID causes OpenCode to abort with `Session ... is not a child of the current session`.
  - Only specify `sessionID` if you are sending a follow-up message to an existing child subagent that was already created.
