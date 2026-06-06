from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, F
from django.utils import timezone
from datetime import timedelta

from decimal import Decimal

from .models import Expense, Tax, RevenueTarget
from .forms import ExpenseForm, RevenueTargetForm
from ..core.decorators import role_required, FINANCE_VIEWERS, MANAGEMENT, OWNER
from ..sales.models import SaleTax, Sale, SaleItem


REVENUE_STATUSES = [Sale.Status.COMPLETED, Sale.Status.PARTIAL_REFUND, Sale.Status.REFUNDED]


@login_required
def expense_list(request):
    """
    List all expenses for the user's location.
    Managers/Owners see all status, Staff see only their requests.
    """
    user = request.user

    # Filter Logic
    if user.role == 'OWNER':
        expenses = Expense.objects.all()
    else:
        expenses = Expense.objects.filter(location=user.assigned_location)

    expenses = expenses.select_related('category', 'requested_by').order_by('-date_incurred', '-created_at')

    # Simple Stats for Header
    today = timezone.now().date()
    month_start = today.replace(day=1)

    total_month = expenses.filter(
        date_incurred__gte=month_start,
        status=Expense.Status.APPROVED
    ).aggregate(Sum('amount'))['amount__sum'] or 0

    pending_count = expenses.filter(status=Expense.Status.PENDING).count()

    return render(request, 'finance/expense_list.html', {
        'expenses': expenses,
        'total_month': total_month,
        'pending_count': pending_count
    })


@login_required
def expense_create(request):
    """
    Log a new expense (e.g. Buying Fuel).
    """
    user = request.user
    if request.method == 'POST':
        form = ExpenseForm(request.POST, request.FILES)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.location = user.assigned_location
            expense.requested_by = user

            # Auto-approve if Owner
            if user.role == 'OWNER':
                expense.status = Expense.Status.APPROVED
                expense.approved_by = user
            else:
                expense.status = Expense.Status.PENDING

            expense.save()
            messages.success(request, "Expense recorded successfully.")
            return redirect('finance:expenses_list')
    else:
        form = ExpenseForm(initial={'date_incurred': timezone.now().date(), 'is_paid_from_till': True})

    return render(request, 'finance/expense_form.html', {'form': form, 'title': 'Record Expense'})


@login_required
@role_required(*MANAGEMENT)
def expense_approve(request, pk):
    """
    Manager/Owner Action: Approve an expense.
    """
    expense = get_object_or_404(Expense, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'approve':
            expense.status = Expense.Status.APPROVED
            expense.approved_by = request.user
            messages.success(request, "Expense approved.")
        elif action == 'reject':
            expense.status = Expense.Status.REJECTED
            messages.warning(request, "Expense rejected.")

        expense.save()

    return redirect('finance:expenses_list')


@login_required
@role_required(*FINANCE_VIEWERS)
def profit_loss_view(request):
    """
    Real-Time Profit & Loss Statement.
    Formula: Net Profit = (Revenue - Cost of Goods Sold) - Operating Expenses
    """
    user = request.user

    # Date Filtering (Default: This Month)
    today = timezone.now().date()
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    if start_date_str:
        start_date = timezone.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    else:
        start_date = today.replace(day=1)

    if end_date_str:
        end_date = timezone.datetime.strptime(end_date_str, "%Y-%m-%d").date()
    else:
        end_date = today

    # Base Filter: Owner sees all, Manager sees assigned location.
    # Include COMPLETED + PARTIAL_REFUND + REFUNDED — we filter refunded LINE ITEMS
    # below so a partially-refunded sale still contributes its non-refunded items.
    revenue_statuses = [
        Sale.Status.COMPLETED,
        Sale.Status.PARTIAL_REFUND,
        Sale.Status.REFUNDED,
    ]

    if user.role == 'OWNER':
        sales_qs = Sale.objects.filter(status__in=revenue_statuses)
        items_qs = SaleItem.objects.filter(sale__status__in=revenue_statuses, is_refunded=False)
        expenses_qs = Expense.objects.filter(status=Expense.Status.APPROVED)
    else:
        loc = user.assigned_location
        sales_qs = Sale.objects.filter(location=loc, status__in=revenue_statuses)
        items_qs = SaleItem.objects.filter(sale__location=loc, sale__status__in=revenue_statuses, is_refunded=False)
        expenses_qs = Expense.objects.filter(location=loc, status=Expense.Status.APPROVED)

    # Apply Date Range
    sales_qs = sales_qs.filter(created_at__date__range=[start_date, end_date])
    items_qs = items_qs.filter(sale__created_at__date__range=[start_date, end_date])
    expenses_qs = expenses_qs.filter(date_incurred__range=[start_date, end_date])

    # 1. Total Revenue — sum of NON-refunded line items (so partial refunds reduce revenue)
    total_revenue = items_qs.aggregate(Sum('total_price'))['total_price__sum'] or 0

    # 2. Cost of Goods Sold (COGS) — same scope as revenue (refunded items aren't sold)
    total_cogs = items_qs.annotate(
        line_cost=F('unit_cost') * F('quantity')
    ).aggregate(Sum('line_cost'))['line_cost__sum'] or 0

    # 3. Gross Profit
    gross_profit = total_revenue - total_cogs

    # 4. Expenses
    total_expenses = expenses_qs.aggregate(Sum('amount'))['amount__sum'] or 0

    # 5. Net Profit
    net_profit = gross_profit - total_expenses

    # Expense Breakdown for Charts/Table
    expense_breakdown = expenses_qs.values('category__name').annotate(total=Sum('amount')).order_by('-total')

    context = {
        'start_date': start_date.strftime("%Y-%m-%d"),
        'end_date': end_date.strftime("%Y-%m-%d"),
        'total_revenue': total_revenue,
        'total_cogs': total_cogs,
        'gross_profit': gross_profit,
        'total_expenses': total_expenses,
        'net_profit': net_profit,
        'expense_breakdown': expense_breakdown
    }
    return render(request, 'finance/profit_loss.html', context)


@login_required
@role_required(*FINANCE_VIEWERS)
def tax_report_view(request):
    """
    Tax Collection Report.
    """
    user = request.user

    # Date Filtering
    today = timezone.now().date()
    start_date = request.GET.get('start_date', today.replace(day=1).strftime("%Y-%m-%d"))
    end_date = request.GET.get('end_date', today.strftime("%Y-%m-%d"))

    # Query Sale Taxes (include partial/full refund statuses; tax is a snapshot at sale time)
    revenue_statuses = [Sale.Status.COMPLETED, Sale.Status.PARTIAL_REFUND, Sale.Status.REFUNDED]
    if user.role == 'OWNER':
        tax_qs = SaleTax.objects.filter(sale__status__in=revenue_statuses)
    else:
        tax_qs = SaleTax.objects.filter(sale__location=user.assigned_location, sale__status__in=revenue_statuses)

    tax_qs = tax_qs.filter(sale__created_at__date__range=[start_date, end_date])

    # Summarize by Tax Name (VAT, NHIL, etc.)
    tax_summary = tax_qs.values('tax_name', 'tax_rate').annotate(total_collected=Sum('tax_amount')).order_by('tax_name')

    total_tax_collected = tax_qs.aggregate(Sum('tax_amount'))['tax_amount__sum'] or 0

    context = {
        'start_date': start_date,
        'end_date': end_date,
        'tax_summary': tax_summary,
        'total_tax_collected': total_tax_collected
    }
    return render(request, 'finance/tax_report.html', context)


@login_required
@role_required(*FINANCE_VIEWERS)
def target_list(request):
    """
    Revenue Targets with live progress against actual (non-refunded) sales.
    Owners see every location's targets; managers see their own location.
    """
    user = request.user
    targets = RevenueTarget.objects.filter(is_active=True).select_related('location').order_by('-start_date')
    if user.role != 'OWNER':
        targets = targets.filter(location=user.assigned_location)

    today = timezone.now().date()
    rows = []
    for t in targets:
        window_end = t.end_date or today
        actual = SaleItem.objects.filter(
            sale__location=t.location,
            sale__status__in=REVENUE_STATUSES,
            is_refunded=False,
            sale__created_at__date__range=[t.start_date, min(window_end, today)],
        ).aggregate(s=Sum('total_price'))['s'] or Decimal('0.00')

        target_amount = t.target_amount or Decimal('0.00')
        pct = float(actual / target_amount * 100) if target_amount > 0 else 0.0
        rows.append({
            'target': t,
            'actual': actual,
            'pct': round(min(pct, 999), 1),
            'pct_bar': round(min(pct, 100), 1),
            'remaining': max(target_amount - actual, Decimal('0.00')),
            'met': actual >= target_amount and target_amount > 0,
        })

    return render(request, 'finance/targets.html', {'rows': rows})


@login_required
@role_required(OWNER)
def target_create(request):
    if request.method == 'POST':
        form = RevenueTargetForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Revenue target created.")
            return redirect('finance:targets')
    else:
        form = RevenueTargetForm(initial={'start_date': timezone.now().date()})
    return render(request, 'finance/target_form.html', {'form': form, 'title': 'Set Revenue Target'})


@login_required
@role_required(OWNER)
def target_delete(request, pk):
    target = get_object_or_404(RevenueTarget, pk=pk)
    if request.method == 'POST':
        target.is_active = False
        target.save()
        messages.success(request, "Target archived.")
    return redirect('finance:targets')



