import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Sum
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import Product, Category, Brand
from .forms import ProductForm, ProductImageForm, CategoryForm, BrandForm
from ..core.decorators import role_required, INVENTORY_STAFF, MANAGEMENT
from ..inventory.models import StockBatch
from ..location.models import Location


@login_required
@role_required(*INVENTORY_STAFF)
def product_list(request):
    """
    The Central Catalog View.
    Features comprehensive search and filtering.
    """
    # 1. Base Query
    products = Product.objects.all().select_related('location', 'category', 'brand', 'unit').prefetch_related('images')

    # Multi-tenant scoping: a shop only sees its own products. Owners see all.
    if request.user.role != 'OWNER':
        products = products.filter(location=request.user.assigned_location)

    # 2. Filtering
    query = request.GET.get('q', '')
    category_id = request.GET.get('category')
    brand_id = request.GET.get('brand')
    status = request.GET.get('status')
    location_id = request.GET.get('location')

    if query:
        products = products.filter(
            Q(name__icontains=query) |
            Q(sku__icontains=query) |
            Q(barcode__icontains=query)
        )

    if category_id:
        products = products.filter(category_id=category_id)

    if brand_id:
        products = products.filter(brand_id=brand_id)

    if status == 'active':
        products = products.filter(is_active=True)
    elif status == 'inactive':
        products = products.filter(is_active=False)

    # Owner-Only Filter: show products belonging to a specific shop
    if request.user.role == 'OWNER' and location_id:
        products = products.filter(location_id=location_id)

    # 3. Sorting
    products = products.order_by('-created_at')

    # 4. Pagination
    paginator = Paginator(products, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # 5. Context Data for Filter Dropdowns
    context = {
        'page_obj': page_obj,
        'query': query,
        'categories': Category.objects.filter(is_active=True),
        'brands': Brand.objects.filter(is_active=True),
        'locations': Location.objects.filter(is_active=True) if request.user.role == 'OWNER' else [],

        # Keep filter state in UI
        'selected_category': int(category_id) if category_id else None,
        'selected_brand': int(brand_id) if brand_id else None,
        'selected_status': status,
        'selected_location': int(location_id) if location_id else None,
    }

    return render(request, 'products/product_list.html', context)


@login_required
@role_required(*INVENTORY_STAFF)
def product_detail(request, pk):
    """
    Detailed view of a product including stock levels across all locations.
    """
    product = get_object_or_404(Product, pk=pk)

    # Multi-tenant scoping: only the owning shop (or an owner) may view it.
    if request.user.role != 'OWNER' and product.location_id != request.user.assigned_location_id:
        messages.error(request, "That product belongs to another shop.")
        return redirect('products:product_list')

    # Get Stock Summary per Location
    # This shows "Shop A: 50 units", "Warehouse: 100 units"
    stock_summary = StockBatch.objects.filter(
        product=product,
        quantity__gt=0
    ).values(
        'location__name', 'location__location_type'
    ).annotate(
        total_qty=Sum('quantity')
    ).order_by('location__name')

    # Calculate Global Total
    total_stock = sum(item['total_qty'] for item in stock_summary)

    # Get recent batches (raw data)
    recent_batches = StockBatch.objects.filter(product=product).order_by('-received_date')[:5]

    context = {
        'product': product,
        'stock_summary': stock_summary,
        'total_stock': total_stock,
        'recent_batches': recent_batches,
    }
    return render(request, 'products/product_detail.html', context)


@login_required
@role_required(*MANAGEMENT)
def product_create(request):
    """
    Create a new product definition.
    """
    is_owner = request.user.role == 'OWNER'
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES)
        if not is_owner:
            form.fields.pop('location', None)  # managers can't reassign shops
            form.instance.location = request.user.assigned_location  # set before validation
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' created successfully.")
            return redirect('products:product_list')
    else:
        form = ProductForm()
        if not is_owner:
            form.fields.pop('location', None)

    return render(request, 'products/product_form.html', {'form': form, 'category_form': CategoryForm(), 'title': 'Add New Product'})


@login_required
@role_required(*MANAGEMENT)
def product_edit(request, pk):
    """
    Update existing product details.
    """
    product = get_object_or_404(Product, pk=pk)
    is_owner = request.user.role == 'OWNER'

    # Managers may only edit their own shop's products.
    if not is_owner and product.location_id != request.user.assigned_location_id:
        messages.error(request, "That product belongs to another shop.")
        return redirect('products:product_list')

    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, instance=product)
        if not is_owner:
            form.fields.pop('location', None)
            form.instance.location = request.user.assigned_location
        if form.is_valid():
            form.save()
            messages.success(request, f"Product '{product.name}' updated.")
            return redirect('products:product_list')
    else:
        form = ProductForm(instance=product)
        if not is_owner:
            form.fields.pop('location', None)

    return render(request, 'products/product_form.html', {'form': form, 'title': f'Edit {product.name}'})


def _barcode_scope(user, location_id=None):
    """
    The products a user may assign barcodes to.

    Managers are locked to their own shop; owners work one shop at a time
    (barcodes are unique *per shop*, so mixing shops in one pass would make
    the duplicate check meaningless).
    """
    products = Product.objects.filter(is_active=True).select_related('location', 'category')
    if _is_owner(user):
        if location_id:
            products = products.filter(location_id=location_id)
        return products
    return products.filter(location=user.assigned_location)


def _is_owner(user):
    """Owners (and superusers) are not pinned to a single shop."""
    return user.role == 'OWNER' or user.is_superuser


@login_required
@role_required(*MANAGEMENT)
def barcode_assign(request):
    """
    Walk the catalogue assigning a scanned barcode to each product.

    One product is 'current' at a time; the scanner fires at a single focused
    input and the page advances to the next product still missing a barcode.
    Saving happens over `barcode_assign_save` so the operator never leaves
    the page or touches the mouse.
    """
    is_owner = _is_owner(request.user)
    locations = Location.objects.filter(is_active=True) if is_owner else []

    selected_location = None
    if is_owner:
        raw = request.GET.get('location')
        if raw and raw.isdigit():
            selected_location = Location.objects.filter(pk=raw).first()
        elif request.user.assigned_location_id:
            selected_location = request.user.assigned_location
        else:
            selected_location = locations.first()

    if not is_owner and not request.user.assigned_location_id:
        messages.error(request, "You need an assigned shop before you can scan barcodes.")
        return redirect('products:product_list')

    if is_owner and selected_location is None:
        messages.error(request, "Create a shop first, then come back to scan barcodes.")
        return redirect('products:product_list')

    location_id = selected_location.id if selected_location else None
    products = _barcode_scope(request.user, location_id).order_by('name')

    rows = [{
        'id': p.id,
        'name': p.name,
        'sku': p.sku,
        'barcode': p.barcode or '',
        'category': p.category.name if p.category else '',
        'price': str(p.selling_price),
    } for p in products]

    done = sum(1 for r in rows if r['barcode'])
    return render(request, 'products/barcode_assign.html', {
        # Handed to the page via {{ rows|json_script }} so a product name
        # containing "</script>" can't break out of the tag.
        'rows': rows,
        'total': len(rows),
        'done': done,
        'is_owner': is_owner,
        'locations': locations,
        'selected_location': selected_location,
        'shop_name': (selected_location or request.user.assigned_location).name
                     if (selected_location or request.user.assigned_location_id) else 'Unassigned',
    })


@login_required
@role_required(*MANAGEMENT, api=True)
@require_POST
def barcode_assign_save(request):
    """
    Save (or clear) one product's barcode. Returns JSON for the scan page.
    """
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': "Malformed request."}, status=400)

    if not _is_owner(request.user) and not request.user.assigned_location_id:
        return JsonResponse({'ok': False, 'error': "You have no assigned shop."}, status=403)

    try:
        product_id = int(data.get('product_id'))
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': "Missing product."}, status=400)
    barcode = (data.get('barcode') or '').strip()

    # Scoped lookup: a manager can never write to another shop's product.
    product = _barcode_scope(request.user).filter(pk=product_id).first()
    if not product:
        return JsonResponse({'ok': False, 'error': "Product not found in your shop."}, status=404)

    if len(barcode) > 100:
        return JsonResponse({'ok': False, 'error': "That barcode is too long (max 100 characters)."}, status=400)

    if barcode:
        # Barcodes are unique per shop — tell the operator exactly what clashed.
        clash = Product.objects.filter(
            location=product.location, barcode=barcode
        ).exclude(pk=product.pk).first()
        if clash:
            return JsonResponse({
                'ok': False,
                'error': f"Already used by {clash.name} ({clash.sku}).",
                'clash_id': clash.id,
            }, status=409)

    product.barcode = barcode or None
    product.save(update_fields=['barcode'])

    done = _barcode_scope(request.user, product.location_id).exclude(
        Q(barcode__isnull=True) | Q(barcode='')
    ).count()
    return JsonResponse({
        'ok': True,
        'product_id': product.id,
        'barcode': product.barcode or '',
        'done': done,
    })


@login_required
@role_required(*MANAGEMENT)
def quick_category_create(request):
    """
    HTMX or Modal view to add a category on the fly while creating a product.
    """
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save()
            # If HTMX, return a partial; if standard, redirect
            messages.success(request, f"Category '{category.name}' added.")
            return redirect('products:product_create')

    return render(request, 'products/partials/category_form.html', {'form': CategoryForm()})