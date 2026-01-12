{
    "name": "Cash Treasury",
    "version": "2.0",
    "category": "Accounting/Treasury",  
    "summary": "Cash Out Management with Review & Posting Workflow",
    "description": """
Cash Treasury module to manage Cash Out vouchers with:
- Draft / Reviewed / Posted workflow
- Role-based access (Entry / Reviewer / Accountant)
- Journal-based numbering
- Automatic journal entries on post
- Full chatter tracking
""",
    "author": "Mohamed Soltan",
    "website": "https://ato-solution.com",
    "license": "LGPL-3",
    "depends": ["base", "account", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/sequence.xml",
        "views/cash_out_view.xml",
	"views/cash_in_view.xml",
	"views/res_users_view.xml",
	"views/menu.xml",
    ],
    "application": True,
    "installable": True,
}
