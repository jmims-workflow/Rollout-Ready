"""Sample criteria the "Draft criteria for me" button returns in demo mode.

They are for the Renewal risk summary example: customer success managers
paste account notes and get a short summary of renewal risk.
"""

SAMPLE_SUITE_NAME = "Renewal risk summary"
SAMPLE_WHO = "Customer success managers, before a renewal call."

SAMPLE_CRITERIA = [
    {
        "name": "Names the real risk",
        "five_looks_like": "Names the one thing most likely to stop this customer renewing, in plain words.",
        "one_looks_like": "Lists every minor complaint, or says the account is at risk without saying why.",
    },
    {
        "name": "Backs it with evidence from the notes",
        "five_looks_like": "Points to specific facts in the notes, like usage drops, a lost champion or open tickets.",
        "one_looks_like": "Makes claims the notes do not support, or gives no evidence at all.",
    },
    {
        "name": "Risk level matches the evidence",
        "five_looks_like": "The low, medium or high rating fits what the notes actually show.",
        "one_looks_like": "Calls a healthy account high risk, or a clearly unhappy one low risk.",
    },
    {
        "name": "One useful action for the next call",
        "five_looks_like": "Suggests one concrete thing the manager can do or ask on the next call.",
        "one_looks_like": "No action, or generic advice like 'keep building the relationship'.",
    },
]
