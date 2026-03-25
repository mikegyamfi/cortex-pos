from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, F
from django.utils import timezone
from datetime import timedelta

from .models import Expense, Tax
from .forms import ExpenseForm
from ..sales.models import SaleTax, Sale, SaleItem


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
def expense_approve(request, pk):
    """
    Manager/Owner Action: Approve an expense.
    """
    expense = get_object_or_404(Expense, pk=pk)

    # Security Check
    if request.user.role not in ['OWNER', 'MANAGER']:
        messages.error(request, "Unauthorized action.")
        return redirect('finance:expenses_list')

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
def profit_loss_view(request):
    """
    Real-Time Profit & Loss Statement.
    Formula: Net Profit = (Revenue - Cost of Goods Sold) - Operating Expenses
    """
    user = request.user
    if user.role not in ['OWNER', 'MANAGER', 'ACCOUNTANT']:
        return redirect('core:dashboard')

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

    # Base Filter: Owner sees all, Manager sees assigned location
    if user.role == 'OWNER':
        sales_qs = Sale.objects.filter(status=Sale.Status.COMPLETED)
        items_qs = SaleItem.objects.filter(sale__status=Sale.Status.COMPLETED)
        expenses_qs = Expense.objects.filter(status=Expense.Status.APPROVED)
    else:
        loc = user.assigned_location
        sales_qs = Sale.objects.filter(location=loc, status=Sale.Status.COMPLETED)
        items_qs = SaleItem.objects.filter(sale__location=loc, sale__status=Sale.Status.COMPLETED)
        expenses_qs = Expense.objects.filter(location=loc, status=Expense.Status.APPROVED)

    # Apply Date Range
    sales_qs = sales_qs.filter(created_at__date__range=[start_date, end_date])
    items_qs = items_qs.filter(sale__created_at__date__range=[start_date, end_date])
    expenses_qs = expenses_qs.filter(date_incurred__range=[start_date, end_date])

    # 1. Total Revenue
    total_revenue = sales_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    # 2. Cost of Goods Sold (COGS)
    # Calculated from unit_cost stored on SaleItem at time of sale
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
def tax_report_view(request):
    """
    Tax Collection Report.
    """
    user = request.user
    if user.role not in ['OWNER', 'MANAGER', 'ACCOUNTANT']:
        return redirect('core:dashboard')

    # Date Filtering
    today = timezone.now().date()
    start_date = request.GET.get('start_date', today.replace(day=1).strftime("%Y-%m-%d"))
    end_date = request.GET.get('end_date', today.strftime("%Y-%m-%d"))

    # Query Sale Taxes
    if user.role == 'OWNER':
        tax_qs = SaleTax.objects.filter(sale__status=Sale.Status.COMPLETED)
    else:
        tax_qs = SaleTax.objects.filter(sale__location=user.assigned_location, sale__status=Sale.Status.COMPLETED)

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



