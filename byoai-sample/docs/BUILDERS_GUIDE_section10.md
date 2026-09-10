## §10 Avaya Infinity: Phone Number Routing

Route an inbound phone number to your workflow so calls reach the IVA module.

1. In the Avaya Infinity Admin Dashboard, navigate to **Voice → Numbers**
2. Click the **+** button to create a new number entry
3. In the **Name** field, enter a descriptive name
4. In the **Phone Number** field, enter the inbound phone number
5. Click **Save**
6. Click the routing button and configure:
   - **Voice Route To**: select **Workflow**
   - **Voice Route Data**: select the workflow you imported in §8
   - **Voice Route Workflow Version**: select **Current**
7. Click **Save**

The number is now routed to your workflow. Inbound calls to this number will enter the workflow and reach the IVA module.

> The IVA module and AI Media Gateway require the call to be routed to a workflow — queue routing bypasses the IVA module entirely.
