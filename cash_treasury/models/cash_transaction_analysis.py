from odoo import models, fields, tools

class CashTransactionAnalysis(models.Model):
    _name = 'cash.transaction.analysis'
    _description = 'Cash Transaction Analysis'
    _auto = False
    _order = 'date'

    date = fields.Date(string='Date')
    journal_id = fields.Many2one('account.journal', string='Journal')
    move_id = fields.Many2one('account.move', string='Journal Entry')
    partner_id = fields.Many2one('res.partner', string='Partner')
    account_id = fields.Many2one('account.account', string='Account')
    label = fields.Char(string='Label')
    debit = fields.Monetary(string='Debit')
    credit = fields.Monetary(string='Credit')

    balance_str = fields.Char(string='Balance')

    currency_id = fields.Many2one('res.currency', string='Currency')

    counter_account_id = fields.Many2one('account.account', string='Counter Account')
    counter_partner_id = fields.Many2one('res.partner', string='Counter Partner')
    counter_label = fields.Char(string='Counter Label')

    def init(self):
        self._cr.execute("""DROP VIEW IF EXISTS cash_transaction_analysis CASCADE""")
        self._cr.execute("""
            CREATE OR REPLACE VIEW cash_transaction_analysis AS (
                WITH cash_per_move AS (
                    SELECT 
                        aml.move_id,
                        aml.account_id,
                        SUM(aml.debit - aml.credit) as total_cash_net
                    FROM account_move_line aml
                    JOIN account_account acc ON aml.account_id = acc.id
                    JOIN account_move am ON aml.move_id = am.id
                    WHERE acc.account_type IN ('asset_cash', 'asset_bank')
                      AND am.state = 'posted'
                      AND EXISTS (
                          SELECT 1
                          FROM account_journal aj
                          WHERE aj.default_account_id = acc.id
                      )
                    GROUP BY aml.move_id, aml.account_id
                ),
                ordered_lines AS (
                    SELECT 
                        aml2.id as line_id,
                        am.date,
                        aml.account_id,
                        am.journal_id,
                        aml.partner_id,
                        aml.move_id,
                        aml.name,
                        aml2.account_id as counter_account_id,
                        aml2.partner_id as counter_partner_id,
                        aml2.name as counter_label,
                        aml2.debit,
                        aml2.credit,
                        aml2.company_currency_id,
                        aml2.credit - aml2.debit as line_amount,
                        ROW_NUMBER() OVER (
                            PARTITION BY aml.account_id 
                            ORDER BY am.date, aml.move_id, aml2.sequence, aml2.id
                        ) as seq_num,
                        SUM(aml2.credit - aml2.debit) OVER (
                            PARTITION BY aml.account_id
                            ORDER BY am.date, aml.move_id, aml2.sequence, aml2.id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) as running_total
                    FROM account_move_line aml
                    JOIN account_account acc ON aml.account_id = acc.id
                    JOIN account_move am ON aml.move_id = am.id
                    JOIN account_move_line aml2 ON aml2.move_id = aml.move_id 
                        AND aml2.account_id != aml.account_id
                    WHERE acc.account_type IN ('asset_cash', 'asset_bank')
                      AND am.state = 'posted'
                      AND EXISTS (
                          SELECT 1
                          FROM account_journal aj
                          WHERE aj.default_account_id = acc.id
                      )
                )
                SELECT 
                    row_number() OVER () as id,
                    ol.date,
                    ol.account_id,
                    ol.journal_id,
                    ol.partner_id,
                    ol.move_id,
                    ol.name as label,
                    ol.counter_account_id,
                    ol.counter_partner_id,
                    ol.counter_label,
                    ol.debit,
                    ol.credit,
                    ol.company_currency_id as currency_id,

                    TO_CHAR(
                        ol.running_total,
                        'FM999,999,999.00'
                    ) as balance_str

                FROM ordered_lines ol
                ORDER BY ol.date, ol.move_id, ol.seq_num
            )
        """)
