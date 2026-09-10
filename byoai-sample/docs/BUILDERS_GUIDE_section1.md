## §1 What You're Building

The contact center has always been where customer relationships are won or
lost. The technology underneath it — the routing, the queuing, the voice
infrastructure — has had to be reliable above all else. Enterprises don't
experiment with mission-critical voice. They standardize, they stabilize,
and they invest for the long term.

That approach made sense when the technology stack changed slowly.

AI doesn't change slowly.

In the last two years, the best conversational AI model has changed hands
multiple times. What was state of the art eighteen months ago is a baseline
today. New providers emerge, capabilities leap forward, and the developers
who bet their architecture on a single model find themselves rebuilding —
not because they made a bad decision, but because the landscape moved.

Avaya sees this differently.

Infinity is built on the principle that your AI strategy should be yours —
not ours, not any single provider's. The BYO AI program is the expression
of that principle in code: a documented, open integration layer that
connects Infinity's contact center platform to any conversational AI you
choose. You bring the model. Infinity handles everything else — the call
routing, the context, the workflow, the handoffs.

This guide is your foundation.

By the time you finish, you'll have a working bridge that connects a live
phone number to a real AI provider — with caller context flowing from your
CRM into the conversation, clean handoffs to human agents, and a call record
that captures everything. You'll have built it on Infinity's RCMS protocol,
which means your Infinity configuration — the workflow, the IVA module, the
routing — doesn't change when you change providers. Swap ElevenLabs for
Gemini. Swap Gemini for whatever ships next year. The bridge pattern stays.
The workflow stays. The integration point is modular by design.

The four providers in this guide — ElevenLabs, Gemini, OpenAI, and xAI —
aren't the point. They're the proof. Proof that the architecture holds across
fundamentally different AI platforms: an agent-as-a-service model, a raw
multimodal model, a realtime API, a voice-native telephony model. Four
different integration patterns, one bridge architecture, one Infinity
workflow.

What you're building isn't four integrations.

It's the foundation for every integration that comes after.

---

### What you'll have when you're done

A running RCMS bridge connected to Avaya Infinity, with:

- A verified end-to-end call flow — live phone number, real AI, real audio
- Caller context from your workflow flowing into every AI conversation
- Clean closure paths — self-service complete, live agent handoff, failure
  recovery — all wired to your workflow branches
- A modular provider architecture you can extend to any LLM

The guide is structured in two parts. Part 1 builds the foundation using
the Echo provider — a loopback that requires no AI credentials and proves
every layer of the stack before any AI complexity is introduced. Part 2
connects a real AI provider. By the end of Part 1, you'll have placed a
call, heard it route through your bridge, and confirmed the integration
works. Everything after that is additive.

Let's build.
