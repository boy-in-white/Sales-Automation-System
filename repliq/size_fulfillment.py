"""Explicit per-product sales-size mapping. Physical stock is never relabelled in SQL."""
import copy
from catalog import SIZES


def previous_size(size):
    return SIZES[SIZES.index(size)-1] if size in SIZES and SIZES.index(size)>0 else None


def sales_rows(rows, product):
    if not product.get('size_substitution_enabled', False):
        return rows
    declared = set(product.get('garment_measurements', {})) | set(product.get('height_guidance', {})) | {r['size'] for r in product.get('sizes', [])}
    declared = declared or {r['size_code'] for r in rows}
    excluded = set(product.get('excluded_sizes', []))
    result=[]
    for size in SIZES:
        if size not in declared or size in excluded:
            continue
        for color in dict.fromkeys(row['color_code'] for row in rows):
            sources=[r for r in rows if r['color_code']==color and r['size_code'] in (size, previous_size(size)) and r['size_code'] not in excluded]
            if not sources:
                continue
            best=max(sources,key=lambda r:(r['available'],r['size_code']==size))
            result.append(dict(best,size_code=size,available=max(r['available'] for r in sources),
                               size_sources=[(r['variant_id'],r['size_code'],r['available']) for r in sources]))
    return result


def allocate(catalog, inventory, items):
    from inventory import StockUnavailable
    result=copy.deepcopy(items); balances={}
    for item in result:
        product=catalog.products[item['product']]
        key=product['key']
        if key not in balances:
            balances[key]=[dict(r) for r in inventory.rows(product) if r['size_code'] not in product.get('excluded_sizes', [])]
        requested=item['size']; fixed=item.get('stock_size')
        allowed=[requested]
        if product.get('size_substitution_enabled') and previous_size(requested) not in product.get('excluded_sizes', []):
            allowed.append(previous_size(requested))
        if fixed:
            allowed=[fixed] if fixed in allowed else []
        chosen=None
        for size in allowed:
            chosen=next((r for r in balances[key] if r['size_code']==size and r['color_code']==item['color'] and r['available']>=item['quantity']),None)
            if chosen:break
        if not chosen:
            raise StockUnavailable('Недостатньо фізичного залишку для всіх позицій замовлення.')
        chosen['available']-=item['quantity']
        if chosen['size_code'] != requested:
            item.update(stock_size=chosen['size_code'],stock_variant_id=str(chosen['variant_id']),size_substitution=True)
        else:
            for field in ('stock_size','stock_variant_id','size_substitution'):item.pop(field,None)
    return result
