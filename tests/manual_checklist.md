# Manual test checklist

Use these steps after deploying the bot to verify target handling and callbacks.

## Public channel/group
1. Start `/report` and load valid sessions.
2. Choose **Public Channel / Group**.
3. Send a public message link like `https://t.me/publicchannel/123`.
4. Confirm that the bot shows target details (title, username/id, type, member count) and proceeds to report.
5. Tap **Back**, **Cancel**, and **Restart** from the inline buttons to ensure callbacks respond.

## Private channel/group (invite required)
1. Start `/report` with valid sessions.
2. Choose **Private Channel / Group**.
3. Send an invite link (`https://t.me/+inviteCode`).
4. Ensure the bot joins successfully, acknowledges the join, and then prompts for the target message link.
5. Send the private message link (`https://t.me/c/<internal_id>/<msg_id>`); verify details are displayed and no `PeerIdInvalid` errors occur.
6. Use **Back/Cancel/Restart** to confirm callbacks remain responsive at each step.

## Profile/user
1. Start `/report` and pick **Story URL (Profile)** or provide a username/link.
2. Send `@username` or `https://t.me/username`.
3. Confirm the bot resolves the profile and sends a details card (name, username, user ID, flags/bio if available).
4. Try the navigation buttons to ensure the flow can be restarted without getting stuck.
