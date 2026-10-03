**Voice Agent: Advisor Appointment Scheduler**

**Milestone brief**

**Create a voice agent that books a tentative advisor slot: collects topic + time preference, offers two slots, confirms, and then creates a calendar hold and notes entry + email draft via MCP. The caller gets a booking code and a secure link to finish details.**

**Who this helps**

**Users who want a human consult; PMs/Support running compliant pre-booking.**

**What you must build**

- **Intents (5): book new, reschedule, cancel, “what to prepare,” check availability windows.**
- **Flow: greet → disclaimer (“informational, not investment advice”) → confirm topic (KYC/Onboarding, SIP/Mandates, Statements/Tax Docs, Withdrawals & Timelines, Account Changes/Nominee) → collect day/time preference → offer two slots (mock calendar) → on confirm:**
    - **Generate Booking Code (e.g., NL-A742).**
    - **MCP Calendar: create tentative hold “Advisor Q&A — {Topic} — {Code}”.**
    - **MCP Notes/Doc: append {date, topic, slot, code} to “Advisor Pre-Bookings”.**
    - **MCP Email Draft: prepare advisor email with details (approval-gated).**
    - **Read the booking code + give a secure URL for contact details (outside the call).**

**Key constraints**

- **No PII on the call (no phone/email/account numbers).**
- **State time zone (IST) and repeat date/time on confirm.**
- **If no slots match → create waitlist hold + draft email.**
- **Refuse investment advice; provide educational links if asked.**
