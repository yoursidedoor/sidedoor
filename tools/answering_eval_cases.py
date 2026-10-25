"""Cases for tools/answering_reasoning_eval.py (owner, 2026-10-24).

Each case is one employer question put to the real answering AI and judge with a profile built
by Setup's own producer (`setup_steps.validate_profile` + `profile_document`). A case lists only
what differs from BASE_FORM:

    form          Setup step 3 fields that differ (places + work_modes make location_priority
                  "<place> <modes>", exactly as setup_steps.profile_document writes it)
    owner_answers answers saved the way Approvals saves them (`setup_steps.save_owner_answer`):
                  (semantic, value, context). Setup has no citizenship pick, so "US citizen" and
                  "green card holder" are recorded this way.
    policy        profile keys Setup does not write (onsite_location_exclusions)
    drop_answers  recorded answers removed after Setup (a profile with "nothing" for that fact)
    review        the role as the browser read it (`review.facts`, the packet's /review)
    question      the employer's question (label, answer_type, choices, semantic)
    expected      {"value": "<the employer's option>"} | {"unknown": true}
                  | {"number": [low, high]} (a figure in that range)
"""

YES_NO = ["Yes", "No"]

RESUME = """JORDAN SAMPLE
Seattle, WA | jordan.sample@example.com | +1 206 555 0143 | linkedin.com/in/jordan-sample

SUMMARY
Backend engineer building data-heavy services in Python on AWS.

EXPERIENCE
Senior Backend Engineer | Northwind Analytics | September 2022 - Present
- Built and run Python (FastAPI, Celery) services processing 2M events a day.
- Moved batch jobs to AWS Lambda and EC2 Auto Scaling groups; cut compute cost 30%.
- Designed PostgreSQL schemas and tuned slow SQL queries for the reporting API.

Software Engineer | Contoso Retail | July 2019 - August 2022
- Maintained Java Spring services for the order pipeline.
- Wrote integration tests and on-call runbooks.

EDUCATION
B.S. Software Engineering | University of Washington | 2019

SKILLS
Python, Java, PostgreSQL, SQL, AWS (Lambda, EC2, S3), Docker, Terraform
"""

BASE_FORM = {
    "given_name": "Jordan", "family_name": "Sample", "email": "jordan.sample@example.com",
    "phone": "+1 206 555 0143", "linkedin": "linkedin.com/in/jordan-sample",
    "current_location": "Seattle, Washington, United States",
    "roles": ["Backend Engineering"], "levels": ["Senior"], "industries": ["AI"],
    "countries": ["United States", "United Kingdom"], "funding_stages": ["Any stage"],
    "minimum_salary": "$180,000", "desired_salary": "$200,000",
    "work_modes": ["Remote"], "places": ["US"],
    "work_authorization": ["United States: allowed to work, no sponsorship needed"],
    "notice_period": "2 weeks", "start_weeks": "2",
    "gender": "Prefer not to say", "race": "Prefer not to say", "veteran": "Prefer not to say",
    "disability": "Prefer not to say",
}

NYC_LONDON = {"places": ["New York, NY", "London"], "work_modes": ["Hybrid", "Onsite"]}
US_CITIZEN = [("us_citizen", True, "Are you a U.S. citizen?")]
GREEN_CARD = [("us_lawful_permanent_resident", True,
               "Are you a U.S. lawful permanent resident (green card holder)?")]


def role(city, country, mode, title="Senior Backend Engineer"):
    return {"facts": {"city": city, "country": country, "work_mode": mode}, "compensation": {},
            "official_url": "https://jobs.example.invalid/apply", "role": title,
            "form_revision": "answering-eval-v1"}


def yes_no(semantic, label):
    return {"semantic": semantic, "label": label, "answer_type": "boolean", "choices": YES_NO}


LONDON = role("London", "GB", "hybrid")
NEW_YORK = role("New York, NY", "US", "hybrid")
US_REMOTE = role(None, "US", "remote")

CASES = [
    {"id": "c01", "form": NYC_LONDON, "review": LONDON,
     "question": yes_no("willing_to_relocate_london", "Are you willing to relocate to London?"),
     "expected": {"value": "Yes"}},
    {"id": "c02", "form": NYC_LONDON, "review": NEW_YORK,
     "question": yes_no("willing_to_relocate_new_york", "Are you willing to relocate to New York?"),
     "expected": {"value": "Yes"}},
    {"id": "c03", "form": NYC_LONDON, "review": LONDON,
     "question": yes_no("currently_lives_within_50_miles_of_london",
                        "Do you currently live within 50 miles of London?"),
     "expected": {"value": "No"}},
    {"id": "c04", "form": {"places": ["London"], "work_modes": ["Hybrid", "Onsite"]}, "review": LONDON,
     "question": yes_no("willing_to_relocate_uk", "Are you willing to relocate to the UK?"),
     "expected": {"value": "Yes"}},
    {"id": "c05", "form": {"places": ["Europe"], "work_modes": ["Onsite", "Hybrid"]},
     "review": role("Berlin", "DE", "onsite"),
     "question": yes_no("willing_to_relocate_berlin", "Would you relocate to Berlin?"),
     "expected": {"value": "Yes"}},
    {"id": "c06", "form": {"places": ["New York, NY"], "work_modes": ["Hybrid"]}, "review": NEW_YORK,
     "question": yes_no("nyc_office_three_days_per_week",
                        "Can you be in our NYC office 3 days per week?"),
     "expected": {"value": "Yes"}},
    {"id": "c07", "form": {"places": ["Anywhere in United States"], "work_modes": ["Remote", "Hybrid", "Onsite"]},
     "policy": {"onsite_location_exclusions": {
         "note": "The owner will not work onsite or hybrid anywhere in Washington state. Remote is unaffected.",
         "work_modes": ["onsite", "hybrid"], "states": ["WA"],
         "state_names": ["Washington", "Washington State"], "cities": [],
         "state_cities": ["Seattle", "Bellevue", "Redmond", "Kirkland", "Tacoma"]}},
     "review": role("Seattle, WA", "US", "onsite"),
     "question": yes_no("open_to_onsite_seattle", "Are you open to working onsite in Seattle?"),
     "expected": {"value": "No"}},
    {"id": "c08", "form": {"places": ["US"], "work_modes": ["Remote"]}, "review": role("Austin, TX", "US", "onsite"),
     "question": yes_no("open_to_onsite_austin", "Are you open to working onsite in Austin, TX?"),
     "expected": {"value": "No"}},
    {"id": "c09", "owner_answers": US_CITIZEN, "review": US_REMOTE,
     "question": yes_no("requires_sponsorship",
                        "Will you now or in the future require visa sponsorship to work in the United States?"),
     "expected": {"value": "No"}},
    {"id": "c10", "owner_answers": US_CITIZEN, "review": LONDON,
     "question": yes_no("authorized_to_work_uk", "Are you legally authorized to work in the United Kingdom?"),
     "expected": {"value": "No"}},
    {"id": "c11", "owner_answers": US_CITIZEN, "review": US_REMOTE,
     "question": yes_no("does_not_require_sponsorship", "Do you NOT require visa sponsorship?"),
     "expected": {"value": "Yes"}},
    {"id": "c12", "owner_answers": GREEN_CARD, "review": US_REMOTE,
     "question": yes_no("sponsorship_now_or_future",
                        "Do you require sponsorship now or in the future?"),
     "expected": {"value": "No"}},
    {"id": "c13", "form": NYC_LONDON, "review": LONDON,
     "question": {"semantic": "expected_annual_salary_gbp", "answer_type": "number", "choices": [],
                  "label": "What is your expected annual base salary in GBP?"},
     "expected": {"number": [120000, 175000]},
     "note": "200k USD is about 150k GBP; a figure like 200000 fails."},
    {"id": "c14", "form": NYC_LONDON, "review": LONDON,
     "question": {"semantic": "expected_hourly_rate_gbp", "answer_type": "number", "choices": [],
                  "label": "What is your expected hourly rate in GBP?"},
     "expected": {"number": [55, 100]},
     "note": "about 150k GBP / 2080 hours = about 72 GBP an hour."},
    {"id": "c15", "review": US_REMOTE,
     "question": yes_no("python_3_plus_years", "Do you have 3+ years of professional Python experience?"),
     "expected": {"value": "Yes"}},
    {"id": "c16", "review": US_REMOTE,
     "question": yes_no("can_start_within_30_days", "Can you start within 30 days of an offer?"),
     "expected": {"value": "Yes"}},
    {"id": "c17", "review": US_REMOTE,
     "question": yes_no("aws_experience", "Do you have experience with AWS?"),
     "expected": {"value": "Yes"}},
    {"id": "c18", "review": US_REMOTE,
     "question": yes_no("bachelors_cs_or_related", "Do you have a Bachelor's degree in Computer Science or a related field?"),
     "expected": {"value": "Yes"}},
    {"id": "c19", "review": US_REMOTE,
     "question": yes_no("sql_experience", "Do you have SQL experience?"),
     "expected": {"value": "Yes"}},
    {"id": "c20", "form": {"places": ["US"], "work_modes": ["Remote"]}, "review": role("Chicago, IL", "US", "onsite"),
     "question": yes_no("able_to_work_onsite", "Are you able to work onsite?"),
     "expected": {"value": "Yes"}, "note": "ability (yes), not willingness (remote-only preference)."},
    {"id": "c21", "form": {"places": ["New York, NY"], "work_modes": ["Hybrid"]}, "review": NEW_YORK,
     "question": yes_no("willing_to_travel_50_percent", "Are you willing to travel up to 50% of the time?"),
     "expected": {"unknown": True}, "note": "nothing records travel: an owner card."},
    {"id": "c22", "form": {"places": ["US"], "work_modes": ["Remote"]}, "review": US_REMOTE,
     "question": yes_no("can_work_eastern_hours", "Can you work Eastern Time business hours?"),
     "expected": {"unknown": True}, "note": "lives on Pacific time; nothing records working hours."},
    {"id": "c26", "form": {"places": ["US"], "work_modes": ["Remote"]}, "review": US_REMOTE,
     "question": yes_no("can_work_european_hours", "Can you work European business hours?"),
     "expected": {"unknown": True}, "note": "owner 2026-10-24: lives in Seattle; a 9-hour shift is a real preference, not inferable."},
    {"id": "c23", "review": US_REMOTE,
     "question": yes_no("active_security_clearance", "Do you hold an active security clearance?"),
     "expected": {"unknown": True}},
    {"id": "c24", "review": US_REMOTE,
     "question": yes_no("valid_drivers_license", "Do you have a valid driver's license?"),
     "expected": {"unknown": True}},
    {"id": "c25", "drop_answers": ["gender_self_identification"], "review": US_REMOTE,
     "question": {"semantic": "eeo_gender", "answer_type": "choice", "label": "Gender",
                  "choices": ["Male", "Female", "Non-binary", "Decline to self-identify"]},
     "expected": {"value": "Decline to self-identify"},
     "note": "answering-prompt.md: no recorded value -> the decline / prefer-not-to-say option."},
]
