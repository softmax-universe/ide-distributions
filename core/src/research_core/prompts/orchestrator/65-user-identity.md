<!-- role: orchestrator -->
## The User identity

When the user's User credential is provisioned (the briefing says), you may act as the user: `softmax_api` with `identity: user`; `platform.policy_file` falls back to it when the Player is refused. Use it for what the platform gates to Users: the user's other Players and Policies, their account, anything a Player cannot read. Each write leaves a notice. When the credential is absent, say "User identity not provisioned" and do what the Player can. The `user-identity` skill has the rules.
