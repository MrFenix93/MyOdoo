from odoo import models, fields, api

class CashTreasuryReportLine(models.Model):
    _name = 'cash.treasury.report.line'
    _description = 'Cash Treasury Report Line'
    _auto = False
    _order = 'date'

    name = fields.Char(string="Reference")
    date = fields.Date(string="Date")
    journal_id = fields.Many2one('account.journal', string="Journal")
    account_id = fields.Many2one('account.account', string="Account")
    move_id = fields.Many2one('account.move', string="Journal Entry")
    move_type = fields.Char(string="Type")
    debit = fields.Float(string="Debit")
    credit = fields.Float(string="Credit")
    balance_str = fields.Char(string="Balance After Move")

    def init(self):
        self._cr.execute("""DROP VIEW IF EXISTS cash_treasury_report_line CASCADE""")
        self._cr.execute("""
            CREATE OR REPLACE VIEW cash_treasury_report_line AS (
                SELECT 
                    aml.id AS id,
                    aml.date AS date,
                    aml.move_id AS move_id,
                    am.name AS name,
                    am.journal_id AS journal_id,
                    am.move_type AS move_type,
                    aml.account_id AS account_id,
                    aml.debit AS debit,
                    aml.credit AS credit,
                    TO_CHAR(
                        SUM(aml.debit - aml.credit) OVER (
                            PARTITION BY aml.account_id
                            ORDER BY aml.date, aml.id
                        ),
                        'FM999,999,999.00'
                    ) AS balance_str
                FROM account_move_line aml
                JOIN account_move am ON aml.move_id = am.id
                JOIN account_account acc ON aml.account_id = acc.id
                WHERE acc.account_type IN ('asset_cash', 'asset_bank')
                  AND am.state = 'posted'
                  AND EXISTS (
                      SELECT 1 
                      FROM account_journal aj 
                      WHERE aj.default_account_id = acc.id
                  )
            );
        """)