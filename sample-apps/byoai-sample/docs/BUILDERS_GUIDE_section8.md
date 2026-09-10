## §8 Avaya Infinity: Workflow

### Import the sample workflow

The sample workflow is pre-configured for the Echo provider. Import it into your Infinity tenant to get a working end-to-end flow without building from scratch.

1. In the Avaya Infinity Admin Dashboard, navigate to **Workflows**
2. Click the **Import** button in the menu bar
3. Select the workflow JSON file from `providers/echo/infinity-workflow/inbound-virtual-agent-echo.json`
4. Click **Import**

The workflow is imported as a draft. Review it in the Workflow Designer before publishing.

---

### Workflow structure

The sample workflow follows this sequence:

**Start → Set Variable (CRM Data) → Set Variable (Source Details) → Intelligent Virtual Agent → Decision (Self Service Complete?) → Create Interaction → End**

The two Set Variable steps populate demo CRM values before the IVA node. In a production deployment replace these with your CRM integration — the variable names (`firstName`, `lastName`, `email`, `caseId`, `caseSubject`) are what the bridge expects in the `bot.start` payload context.
