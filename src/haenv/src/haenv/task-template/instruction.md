# HAEnv Clinical Reasoning Task (Diagnosis Track — M2)

{question}

---

## Harbor Execution & EHR Tool Instructions

Patient records and the budgeted investigation menu are served by an isolated EHR service. Use the `haenv-ehr` CLI (JSON output on stdout) to query available records up to index time `T` and to order tests within the declared purchase budget:

```bash
# Inspect available sources, patient profile, and records
haenv-ehr list-sources
haenv-ehr get-profile
haenv-ehr get-labs [--name <lab_name>] [--from-day <int>] [--to-day <int>]
haenv-ehr get-series <stream_name> [--from-day <int>] [--to-day <int>]
haenv-ehr get-meds [--from-day <int>] [--to-day <int>]
haenv-ehr get-notes [--kind <source_type>] [--from-day <int>] [--to-day <int>]
haenv-ehr get-record <section_name> [--from-day <int>] [--to-day <int>]
haenv-ehr get-protocol [--id <protocol_id>]

# View the priced investigation menu and remaining budget, then order tests
haenv-ehr get-menu
haenv-ehr order-test <target_1> [<target_2> ...]
```

### Final Submission

When you have finished your workup, write **exactly one JSON object** matching the schema above to:

```text
/workspace/answer.json
```

Do not wrap the file contents in markdown fences. Include your management plan (addressing decisive history if any, concrete next steps, follow-up interval and return precautions, and grounded explanation) in the `management_plan` and `action` fields.
