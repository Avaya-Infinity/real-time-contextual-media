# Infinity RCMS BYO AI — Builder's Guide

> Start here. Every provider guide assumes you have completed this guide first.

## Service availability

Of the services modeled by Real-time Contextual Media Streaming, **Virtual
Agent (bidirectional conversational AI) is the only one generally available for
BYO AI partners today** — which is why it is the only service this guide and the
sample repository build against. Other modeled services (Agent Assist,
Recording, Transcription, Text-to-Speech, Speech Recognition, Translation) are
on the roadmap but are not yet ready to build against. The scope here reflects
platform readiness, not a limitation of the bridge.

## Part 1 — The Foundation

Build and validate the complete bridge + Infinity integration using the Echo
provider. Echo requires no AI credentials and no external service — it proves
your foundation is working before any AI complexity is introduced.

- [§1 What You're Building](docs/BUILDERS_GUIDE_section1.md)
- [§2 Architecture](docs/BUILDERS_GUIDE_section2.md)
- [§3 The Call Lifecycle](docs/BUILDERS_GUIDE_section3.md)
- [§4 Bridge Installation and Configuration](docs/BUILDERS_GUIDE_section4.md)
- [§5 Avaya Infinity: Network Requirements](docs/BUILDERS_GUIDE_section5.md)
- [§6 Avaya Infinity: Security Keys & JWT](docs/BUILDERS_GUIDE_section6.md)
- [§7 Avaya Infinity: BYO AI Integration](docs/BUILDERS_GUIDE_section7.md)
- [§8 Avaya Infinity: Workflow](docs/BUILDERS_GUIDE_section8.md)
- [§9 Avaya Infinity: IVA Module](docs/BUILDERS_GUIDE_section9.md)
- [§10 Avaya Infinity: Phone Number Routing](docs/BUILDERS_GUIDE_section10.md)
- [§11 Verification with Echo](docs/BUILDERS_GUIDE_section11.md)
- [§12 Quick Reference](docs/BUILDERS_GUIDE_section12.md)

## Part 2 — AI Providers

Your foundation is working. Pick a provider and follow their guide.
Each provider guide covers: configuration, context injection, Phase 2 audio
and barge-in behavior, Phase 3 closure paths, workflow, and known behaviors.

| Provider | Guide | Workflow |
|---|---|---|
| Echo | [Echo Guide](docs/guides/echo.md) | [Echo Workflow](infinity-workflow/echo/) |
| ElevenLabs | [ElevenLabs Guide](docs/guides/elevenlabs.md) | [ElevenLabs Workflow](infinity-workflow/elevenlabs/) |
| Gemini | [Gemini Guide](docs/guides/gemini.md) | [Gemini Workflow](infinity-workflow/gemini/) |
| OpenAI | [OpenAI Guide](docs/guides/openai.md) | [OpenAI Workflow](infinity-workflow/openai/) |
| xAI Grok | [xAI Guide](docs/guides/xai.md) | [xAI Workflow](infinity-workflow/xai/) |

## Part 3 — Adding a New Provider

The reference implementation ships five providers. This section explains
how to build your own.

- [Adding a Provider](docs/ADDING_A_PROVIDER.md)
