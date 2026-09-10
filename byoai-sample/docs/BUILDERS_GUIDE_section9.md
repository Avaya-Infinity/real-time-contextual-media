## §9 Avaya Infinity: IVA Module

### IVA module configuration

The Intelligent Virtual Agent module is the step in the workflow that invokes the bridge. Open the IVA node in the Workflow Designer to review its configuration.

**Connection:**

| Field | Value |
|---|---|
| Connection Type | AI Media Gateway |
| Connection | Your AI Media Gateway profile from §7 |

**Bot ID:**

The `botId` custom parameter tells the bridge which provider to route the call to. For the Echo provider set it to `echo`. For AI providers the value follows the `<prefix>:<model>` convention — for example `gemini:gemini-3.1-flash-live-preview`. Each provider guide covers the correct Bot ID value.

**Custom Parameters:**

The IVA module passes context to the bridge at call time via custom parameters. The sample workflow passes:

| Parameter | Value | Description |
|---|---|---|
| `botId` | `echo` | Routes the call to the correct provider plugin. See Bot ID above. |
| `firstName` | `{{firstName}}` | Caller first name — resolved from CRM data in the workflow |
| `lastName` | `{{lastName}}` | Caller last name — resolved from CRM data in the workflow |
| `email` | `{{email}}` | Caller email address — resolved from CRM data in the workflow |
| `caseId` | `{{caseId}}` | Active case identifier — resolved from CRM data in the workflow |
| `caseSubject` | `{{caseSubject}}` | Case subject — resolved from CRM data in the workflow |
| `caseDescription` | `{{caseDescription}}` | Case description — resolved from CRM data in the workflow |
| `engagementId` | `{{engagementId}}` | Unique identifier for this interaction assigned by Avaya Infinity. Persists for the lifetime of the interaction — use this to correlate the AI session with the broader interaction record in reporting and CRM systems. |
| `workflowSessionId` | `{{workflowSessionId}}` | Unique identifier for this workflow execution instance. Useful for correlating bridge session logs with workflow execution traces in Infinity reporting. |

You can add, remove, or rename parameters to match your use case — the bridge passes whatever is present in `customParameters` to the provider plugin.

---

### IVA module exits

The IVA module has three exit paths:

**SUCCESSFUL** — the bridge sent `bot.ended` with a `200` status code. The sample workflow routes this to a Decision node that checks `{{variables.byobotEndContext.status.code}}` equals `200` to confirm self-service completion before proceeding.

**HANDOFF** — the bridge sent a `bot.feature LIVE_AGENT_HANDOFF` event followed by `bot.ended`. The call is ready to be routed to a live agent. The sample workflow routes this to Create Interaction — replace with your live agent routing logic.

**FAILED** — the bridge sent `bot.ended` with a non-200 status code. This covers provider errors, configuration problems, and session failures. The sample workflow routes this to Create Interaction — replace with your error handling logic.

---

### Handoff events

The IVA module has two event flags:

**Enable Handoff Events** — must be set to `true` for live agent handoff to work. When enabled, the `HANDOFF` exit fires when the bridge emits `bot.feature LIVE_AGENT_HANDOFF`. The handoff payload carries the queue ID, tags, and reason — available in the workflow as `{{variables.byobotLiveAgentHandoff}}`.

**Enable Transfer Call Events** — used when transferring a call to a destination outside Avaya Infinity, such as an external phone number or a third-party system. This is separate from the live agent handoff path which routes within Infinity. Leave this disabled unless your use case requires external transfer.
