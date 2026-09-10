## §7 Avaya Infinity: BYO AI Integration

### Create an AI Media Gateway Profile

The AI Media Gateway profile tells Avaya Infinity where your bridge is and how to authenticate with it.

1. In the Avaya Infinity Admin Dashboard, navigate to **Voice → AI Media Gateway Profiles**
2. Click the **+** button in the menu bar to create a new profile
3. In the **Name** field, enter a descriptive name (e.g. `RCMS Bridge - Production`)
4. In the **Service Types** field, select the required service types for your integration
5. In the **Key Pair** field, select the authentication key pair you created in §6. If you have not created one yet, follow the steps in §6 first.
6. In the **Destination WebSocket URL** field, enter your bridge's public HTTPS URL — for example:
   ```
   wss://your-bridge-domain.example.com
   ```
7. Click **Create**

The profile is created and enabled immediately.

> **The destination URL is static per profile.** Avaya Infinity does not
> template per-call identifiers into the URL path — the same WSS endpoint is
> used for every call on this profile. To route or identify calls (by org,
> channel, session, etc.), carry that data as **context** rather than in the
> URL: pass it via IVA Custom Parameters (see §9), where it arrives in the
> `bot.start` payload for your provider plugin to act on. For multi-org or
> multi-tenant deployments, encode the routing key in `botId` and/or a custom
> parameter and branch on it inside the bridge — one static endpoint, many
> logical destinations.

> **How many profiles can I create?** There is no limit on the number of AI
> Media Gateway Profiles. Profiles are not license-gated or metered — create as
> many as your routing model needs (for example, one per environment, per
> business unit, or per bridge endpoint).

---

### Passing context to the bridge

Avaya Infinity gives you two places to pass context to your bridge at call time:

**At the AI Media Gateway profile level** — use Input Variables on the profile to define key-value pairs that apply to every call using this profile. These are well-suited for static values or environment-level configuration that does not change per call.

**At the IVA module level in Workflows** — use the Custom Parameters on the IVA module to pass call-specific context resolved from workflow and CRM variables. This is the recommended approach when context varies per caller. The sample application uses this method — see §9 for details.

Both sources arrive at the bridge in the `bot.start` message payload and are available to your provider plugin for prompt injection, routing decisions, and dynamic variable substitution.
