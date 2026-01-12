from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
from odoo.tools.float_utils import float_compare


# =====================================================
# CASH IN
# =====================================================
class CashTreasuryIn(models.Model):
    _name = "cash.treasury.in"
    _description = "Cash In"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(readonly=True, copy=False)

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("approved", "Approved (Ready to Post)"),
            ("posted", "Posted"),
        ],
        default="draft",
        tracking=True,
    )

    receive_from_type = fields.Selection(
        [
            ("account", "Account"),
            ("partner", "Partner"),
        ],
        required=True,
        default="account",
    )

    partner_id = fields.Many2one("res.partner")
    account_id = fields.Many2one("account.account")

    amount = fields.Monetary(required=True)
    date = fields.Date(default=fields.Date.context_today, required=True, tracking=True)
    notes = fields.Text()

    # actual collection date (used for posting)
    collection_date = fields.Date(tracking=True)

    journal_id = fields.Many2one(
        "account.journal",
        required=True,
        domain=lambda self: self._get_journal_domain(),
    )

    payment_method_id = fields.Many2one(
        "account.payment.method",
        required=True,
        domain="[('payment_type','=','inbound')]",
    )

    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        required=True,
    )

    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        readonly=True,
    )

    destination_account_id = fields.Many2one(
        "account.account",
        compute="_compute_destination_account",
        store=True,
    )

    # Allocation with customer invoices
    invoices_loaded = fields.Boolean(default=False, copy=False)

    allocation_line_ids = fields.One2many(
        "cash.treasury.in.allocation",
        "cash_in_id",
        copy=False,
    )

    total_allocated = fields.Monetary(
        compute="_compute_totals",
        currency_field="currency_id",
    )

    allocation_diff = fields.Monetary(
        compute="_compute_totals",
        currency_field="currency_id",
    )

    journal_entry_id = fields.Many2one("account.move", readonly=True)
    reversal_entry_id = fields.Many2one("account.move", readonly=True, copy=False)

    # =================================================
    # DOMAIN METHOD FOR JOURNAL FILTERING
    # =================================================
    def _get_journal_domain(self):
        """Filter journals based on user permissions"""
        try:
            # ÌíÈ ÇáíæÒÑ Çááí ÚÇãá action
            user = self.env.user
            
            # Super users (Admin) see all cash/bank journals
            if user.has_group('base.group_system'):
                return [('type', 'in', ('cash', 'bank'))]
            
            # Regular users see only journals they have permission for
            journal_ids = user.cash_treasury_journal_ids.ids
            
            # If no journals assigned, show none
            if not journal_ids:
                return [('id', '=', False)]  # Empty domain
            
            return [('id', 'in', journal_ids), ('type', 'in', ('cash', 'bank'))]
        
        except Exception as e:
            # Fallback if something goes wrong
            import logging
            logging.error("Journal domain error: %s", str(e))
            return [('type', 'in', ('cash', 'bank'))]

    # =================================================
    # ONCHANGE FOR JOURNAL
    # =================================================
    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        """Verify user has permission for selected journal"""
        # Admin can select any journal without warning
        if self.env.user.has_group('base.group_system'):
            return  # ÇáÜ Admin íãÔí ÈÏæä ÊÍÐíÑ
        
        # For non-admin users: check permissions
        if self.journal_id and self.env.user.id:
            user_journals = self.env.user.cash_treasury_journal_ids.ids
            if self.journal_id.id not in user_journals:
                warning = {
                    'title': 'Permission Denied',
                    'message': 'You do not have permission to use this journal. Please select from your allowed journals.'
                }
                self.journal_id = False
                return {'warning': warning}

    # =================================================
    # HARD LOCK (ALLOW WORKFLOW ONLY)
    # =================================================
    def write(self, vals):
        """
        Rules:
        - Draft: editable by Cash In Entry (ACL controls)
        - Approved/Posted: read-only (except workflow fields)
        - Approved: Cash In Entry can set collection_date only
        - Super Approver: can bypass lock (used for cancel posted -> draft)
        """
        # Super Approver bypass (ONE shared group)
        if (
            self.env.user.has_group("cash_treasury.group_cash_in_accountant")
            and set(vals.keys()).issubset({"state", "collection_date"})
        ):
            return super().write(vals)  

        # Cash In Accountant can change state only
        if self.env.user.has_group("cash_treasury.group_cash_in_accountant"):
            allowed = {
                "state",
                "journal_entry_id",
                "reversal_entry_id",
                "name",
                "collection_date",
            }
            if set(vals.keys()).issubset(allowed):
                return super().write(vals)
            
        allowed_with_state = {
            "state",
            "collection_date",
            "journal_entry_id",
            "reversal_entry_id",
            "name",
            # chatter / technical
            "write_date",
            "message_ids",
            "message_follower_ids",
        }

        allowed_no_state = {
            "journal_entry_id",
            "reversal_entry_id",
            "name",
            "write_date",
            "message_ids",
            "message_follower_ids",
        }

        for rec in self:
            # Allow collection_date edit in Approved for Cash In Entry only
            if rec.state == "approved":
                if (
                    self.env.user.has_group("cash_treasury.group_cash_in_entry")
                    and set(vals.keys()) == {"collection_date"}
                ):
                    continue

            if rec.state != "draft":
                if "state" in vals:
                    if not set(vals.keys()).issubset(allowed_with_state):
                        raise UserError("Modification is only allowed in Draft state.")
                else:
                    if not set(vals.keys()).issubset(allowed_no_state):
                        raise UserError("Modification is only allowed in Draft state.")

        return super().write(vals)

    def unlink(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("You can only delete a Cash In document in Draft state.")
        return super().unlink()

    # =================================================
    # COMPUTES
    # =================================================
    @api.depends("receive_from_type", "account_id", "partner_id")
    def _compute_destination_account(self):
        for rec in self:
            if rec.receive_from_type == "account":
                rec.destination_account_id = rec.account_id
            else:
                rec.destination_account_id = (
                    rec.partner_id.property_account_receivable_id
                    if rec.partner_id
                    else False
                )

    @api.depends("amount", "allocation_line_ids.amount_to_collect")
    def _compute_totals(self):
        for rec in self:
            total = sum(l.amount_to_collect or 0.0 for l in rec.allocation_line_ids)
            rec.total_allocated = total
            rec.allocation_diff = (rec.amount or 0.0) - total

    # =================================================
    # ONCHANGE
    # =================================================
    @api.onchange("receive_from_type")
    def _onchange_receive_from_type(self):
        if self.receive_from_type == "account":
            self.partner_id = False
            self.invoices_loaded = False
            self.allocation_line_ids = [(5, 0, 0)]
        else:
            self.account_id = False

    @api.onchange("partner_id")
    def _onchange_partner(self):
        if self.invoices_loaded:
            self.invoices_loaded = False
            self.allocation_line_ids = [(5, 0, 0)]

    # =================================================
    # CREATE METHOD
    # =================================================
    @api.model
    def create(self, vals):
        # Auto-set journal if not provided and user has only one
        if 'journal_id' not in vals:
            user_journals = self.env.user.cash_treasury_journal_ids
            if len(user_journals) == 1:
                vals['journal_id'] = user_journals[0].id
            elif len(user_journals) == 0:
                raise UserError("You don't have permission to use any cash journal. Please contact administrator.")
        
        return super(CashTreasuryIn, self).create(vals)

    # =================================================
    # BUTTON: LOAD CUSTOMER INVOICES
    # =================================================
    def action_load_customer_invoices(self):
        for rec in self:
            if rec.receive_from_type != "partner":
                raise UserError("Load Customer Invoices is only allowed for Partner receipts.")
            if not rec.partner_id:
                raise UserError("Please select a Partner first.")
            if rec.state != "draft":
                raise UserError("You can only load invoices in Draft state.")

            invoices = self.env["account.move"].search(
                [
                    ("move_type", "=", "out_invoice"),
                    ("state", "=", "posted"),
                    ("partner_id", "=", rec.partner_id.id),
                    ("amount_residual", ">", 0),
                ],
                order="invoice_date asc, id asc",
            )

            rec.allocation_line_ids = [(5, 0, 0)]

            lines = []
            for inv in invoices:
                lines.append(
                    (
                        0,
                        0,
                        {
                            "invoice_id": inv.id,
                            "selected": False,
                            "amount_to_collect": 0.0,
                        },
                    )
                )

            rec.allocation_line_ids = lines
            rec.invoices_loaded = True

    # =================================================
    # VALIDATIONS
    # =================================================
    
    @api.constrains("amount")
    def _check_amount_positive(self):
        for rec in self:
            if rec.amount is None or rec.amount <= 0:
                raise ValidationError(_("Amount must be greater than zero."))
        
    @api.constrains("state", "invoices_loaded", "allocation_line_ids.amount_to_collect", "amount")
    def _check_diff_when_not_draft(self):
        for rec in self:
            if rec.state == "draft":
                continue
            if not rec.invoices_loaded:
                continue

            total = sum(l.amount_to_collect or 0.0 for l in rec.allocation_line_ids)
            if float_compare(
                total,
                rec.amount or 0.0,
                precision_rounding=rec.currency_id.rounding,
            ) != 0:
                raise ValidationError(
                    "When customer invoices are loaded, total allocated must equal Cash In amount."
                )

    # =================================================
    # WORKFLOW
    # =================================================
    def action_approve(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("Only draft records can be approved.")
            # Accountant approves, then entry sets collection_date later
            rec.write({"state": "approved", "collection_date": False})

    def action_back_to_draft(self):
        """
        Accountant: back to draft BEFORE posting (approved -> draft)
        """
        for rec in self:
            if rec.state != "approved":
                raise UserError("Only approved records can be reset to draft.")
            rec.write(
                {
                    "state": "draft",
                    "journal_entry_id": False,
                    "reversal_entry_id": False,
                    "name": False,
                    "collection_date": False,
                }
            )

    # =================================================
    # POST (CREATE ENTRY + RECONCILE)
    # =================================================
    def action_post(self):
        for rec in self:
            if rec.state != "approved":
                raise UserError("Only approved records can be posted.")
            if not rec.collection_date:
                raise UserError("Please set the Collection Date before posting.")

            # ---------- SEQUENCE (on posting) ----------
            Sequence = self.env["ir.sequence"].sudo()

            seq_code = f"cash.in.{rec.journal_id.id}"

            sequence = Sequence.search([("code", "=", seq_code)], limit=1)
            if not sequence:
                sequence = Sequence.create(
                    {
                        "name": f"Cash In {rec.journal_id.name}",
                        "code": seq_code,
                        "prefix": f"{rec.journal_id.code}/IN/%(year)s-%(month)s/",
                        "padding": 4,
                        "company_id": rec.company_id.id,
                    }
                )

            seq_name = sequence.with_context(ir_sequence_date=rec.collection_date).next_by_id()

            debit_account = rec.journal_id.default_account_id
            if not debit_account:
                raise UserError("Journal has no default account.")

            credit_account = (
                rec.partner_id.property_account_receivable_id
                if rec.receive_from_type == "partner"
                else rec.account_id
            )
            if not credit_account:
                raise UserError("Missing source account.")

            lines = []

            # ---------- DEBIT LINE (Cash/Bank) ----------
            lines.append(
                (
                    0,
                    0,
                    {
                        "account_id": debit_account.id,
                        "debit": rec.amount,
                        "credit": 0.0,
                        "name": seq_name,
                    },
                )
            )

            # ---------- WITH INVOICES ----------
            if rec.invoices_loaded:
                allocations = rec.allocation_line_ids.filtered(
                    lambda l: l.selected and l.amount_to_collect > 0
                )
                if not allocations:
                    raise UserError("Please select invoices and enter amounts.")

                total = sum(al.amount_to_collect for al in allocations)

                if float_compare(
                    total,
                    rec.amount or 0.0,
                    precision_rounding=rec.currency_id.rounding,
                ) != 0:
                    raise UserError("Allocated total must equal Cash In amount.")

                for al in allocations:
                    lines.append(
                        (
                            0,
                            0,
                            {
                                "account_id": credit_account.id,
                                "partner_id": rec.partner_id.id,
                                "credit": al.amount_to_collect,
                                "debit": 0.0,
                                "name": seq_name,
                            },
                        )
                    )

            # ---------- WITHOUT INVOICES ----------
            else:
                lines.append(
                    (
                        0,
                        0,
                        {
                            "account_id": credit_account.id,
                            "partner_id": rec.partner_id.id if rec.receive_from_type == "partner" else False,
                            "credit": rec.amount,
                            "debit": 0.0,
                            "name": seq_name,
                        },
                    )
                )

            move = self.env["account.move"].create(
                {
                    "move_type": "entry",
                    "journal_id": rec.journal_id.id,
                    "date": rec.collection_date,
                    "ref": seq_name,
                    "line_ids": lines,
                }
            )
            move.action_post()

            # ---------- RECONCILE ----------
            if rec.invoices_loaded:
                for al in rec.allocation_line_ids.filtered(
                    lambda l: l.selected and l.amount_to_collect > 0
                ):
                    inv_lines = al.invoice_id.line_ids.filtered(
                        lambda l: l.account_id.id == credit_account.id and not l.reconciled
                    )
                    pay_lines = move.line_ids.filtered(
                        lambda l: l.account_id.id == credit_account.id and not l.reconciled
                    )
                    (inv_lines + pay_lines).reconcile()

            rec.write(
                {
                    "name": seq_name,
                    "journal_entry_id": move.id,
                    "state": "posted",
                }
            )

    # =================================================
    # SUPER APPROVER: CANCEL POSTED -> DRAFT (REVERSAL ENTRY + UNRECONCILE)
    # =================================================
    def action_super_cancel_posted_to_draft(self):
        if not self.env.user.has_group("cash_treasury.group_cash_super_approver"):
            raise UserError("You are not allowed to perform this action.")

        not_posted = self.filtered(lambda r: r.state != "posted")
        if not_posted:
            raise UserError("All selected Cash In records must be in Posted state.")

        today = fields.Date.context_today(self)

        for rec in self:
            move = rec.journal_entry_id
            if not move:
                raise UserError("No journal entry found on this document.")

            # 1) Unreconcile move lines
            for line in move.line_ids:
                if line.reconciled:
                    line.remove_move_reconcile()

            # 2) Create reversal move
            rev_lines = []
            for line in move.line_ids:
                vals = {
                    "account_id": line.account_id.id,
                    "partner_id": line.partner_id.id if line.partner_id else False,
                    "debit": line.credit,
                    "credit": line.debit,
                    "name": f"Reversal of {move.name or move.ref or rec.name}",
                }
                if line.currency_id:
                    vals["currency_id"] = line.currency_id.id
                    vals["amount_currency"] = -line.amount_currency
                rev_lines.append((0, 0, vals))

            rev_move = self.env["account.move"].create(
                {
                    "move_type": "entry",
                    "journal_id": move.journal_id.id,
                    "date": today,
                    "ref": f"Reversal of {move.ref or move.name or rec.name}",
                    "line_ids": rev_lines,
                }
            )
            rev_move.action_post()

            rec.reversal_entry_id = rev_move.id

            # 3) Back to draft
            rec.write(
                {
                    "state": "draft",
                    "journal_entry_id": False,
                    "name": False,
                    "collection_date": False,
                }
            )

            rec.message_post(body="Posted entry reversed by Super Approver and document returned to Draft.")

        return True

    def action_submit(self):
        for rec in self:
            if rec.state != "draft":
                continue
            rec.state = "approved"


# =====================================================
# ALLOCATION LINE (Customer Invoices)
# =====================================================
class CashTreasuryInAllocation(models.Model):
    _name = "cash.treasury.in.allocation"
    _description = "Cash In Allocation Line"

    cash_in_id = fields.Many2one(
        "cash.treasury.in",
        required=True,
        ondelete="cascade",
    )

    selected = fields.Boolean(default=False)

    invoice_id = fields.Many2one(
        "account.move",
        domain="[('move_type','=','out_invoice'),('state','=','posted')]",
    )

    name = fields.Char(compute="_compute_invoice", store=True)

    residual_amount = fields.Monetary(
        compute="_compute_invoice",
        store=True,
        currency_field="currency_id",
    )

    amount_to_collect = fields.Monetary(currency_field="currency_id")

    currency_id = fields.Many2one(
        "res.currency",
        related="cash_in_id.currency_id",
        readonly=True,
    )

    @api.depends("invoice_id", "invoice_id.amount_residual", "invoice_id.name")
    def _compute_invoice(self):
        for rec in self:
            if rec.invoice_id:
                rec.name = rec.invoice_id.name
                rec.residual_amount = rec.invoice_id.amount_residual
            else:
                rec.name = False
                rec.residual_amount = 0.0

    @api.onchange("amount_to_collect")
    def _onchange_amount_to_collect(self):
        if not self.selected and self.amount_to_collect:
            self.amount_to_collect = 0.0
            return {
                "warning": {
                    "title": "Not Allowed",
                    "message": "You must select the invoice before entering an amount.",
                }
            }

        if (
            self.invoice_id
            and self.amount_to_collect
            and self.amount_to_collect > self.invoice_id.amount_residual
        ):
            self.amount_to_collect = self.invoice_id.amount_residual
            return {
                "warning": {
                    "title": "Invalid Amount",
                    "message": "Amount cannot exceed invoice residual.",
                }
            }

    @api.onchange("selected")
    def _onchange_selected(self):
        if not self.selected:
            self.amount_to_collect = 0.0
        elif self.invoice_id:
            self.amount_to_collect = self.invoice_id.amount_residual