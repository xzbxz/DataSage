"""Pure legacy reminder presentation; keep governed values and unknowns intact.

Source: release/datasage-0.2.0 send_slow_report.py / ready_goods_price_push.py.
Unlike the old integer rounding, confirmed fractional roll values are preserved.
"""
from datetime import date,timedelta
from decimal import Decimal

def number(value):
    try:
        result=Decimal(str(value));return result if result.is_finite() else None
    except Exception:return None
def display(value):
    value=number(value)
    if value is None:return 'Unknown'
    text=format(value,'f')
    if '.' in text:text=text.rstrip('0').rstrip('.')
    if text.lstrip('-').isdigit():return f"{int(text):,}"
    sign='-' if text.startswith('-') else '';body=text[1:] if sign else text
    whole,fraction=body.split('.',1);return sign+f"{int(whole):,}.{fraction}"
def quantity(values):
    """Render HT quantities in the old ``value unit; ...`` shape.

    A missing unit value remains visible as Unknown.  Known zero values follow
    the historical ``(none)`` display; the governed numeric claim remains
    available in the input summary and is never changed here.
    """
    if not isinstance(values,dict) or not values:return 'Unknown'
    parts=[]
    for unit,value in sorted(values.items(),key=lambda item:str(item[0])):
        unit=str(unit or 'Unknown').strip() or 'Unknown'
        unit={'m':'M','y':'Y','unknown':'Unknown'}.get(unit.lower(),unit.lower())
        parsed=number(value)
        if parsed is None:
            parts.append('Unknown '+unit)
        elif parsed!=0:
            parts.append((display(parsed)+' '+unit).strip())
    return '; '.join(parts) if parts else '(none)'

def report(region,period,summary,sales_rows,monthly=False,weekly_start=None,detail_semantics=None):
    opening=number(summary.get('opening_skus'));closing=number(summary.get('closing_skus'))
    old_rolls=number(summary.get('opening_rolls'));new_rolls=number(summary.get('closing_rolls'))
    sd=closing-opening if opening is not None and closing is not None else None
    rd=new_rolls-old_rolls if old_rolls is not None and new_rolls is not None else None
    def arrow(value):return 'Unknown' if value is None else '↑'+display(value) if value>0 else '↓'+display(-value) if value<0 else 'flat'
    if sd is None or rd is None:word,color='unassessable','comment'
    else:
        improved=sd<0 or rd<0;worsened=sd>0 or rd>0
        word,color=('improved','info') if improved and not worsened else ('worsened','warning') if worsened and not improved else ('mixed','comment')
    sku_unit='SKU' if sd is not None and abs(sd) in (0,1) else 'SKUs'
    if monthly:
        title=f'**{region} Slow-moving Inventory Monthly Report | {period}**'
        subtitle='> period since month-begin (opening pool = previous month-end snapshot, ODS whitelist excluded)'
    else:
        year,week=period.split('-W')
        monday=date.fromisocalendar(int(year),int(week),1)
        start=date.fromisoformat(str(weekly_start)[:10]) if weekly_start else monday
        end=monday+timedelta(days=5)
        title=f'**{region} Slow-moving Inventory Weekly Report | W{week}**'
        subtitle=f'> {start:%Y/%m/%d} - {end:%Y/%m/%d}'
    verdict=f'<font color="{color}">**Slow-moving stock {word}: {sku_unit} {arrow(sd)} | Rolls {arrow(rd)}**</font>'
    lines=[title,subtitle,'',verdict,'','**1. Key Metrics'+(' (Month-to-date)' if monthly else '')+'**',
        f'　SKUs: **{display(opening)}** -> **{display(closing)}**  {arrow(sd)}',
        f'　Rolls: **{display(old_rolls)}** -> **{display(new_rolls)}**  {arrow(rd)}',
        f'　New SKUs: **{display(summary.get("new"))}**',f'　Exited SKUs: **{display(summary.get("exited"))}**',
        '','**2. Sold This '+('Month' if monthly else 'Week')+'**']
    if region.endswith('-HT'):
        lines += ['　Total: **'+quantity(summary.get('net_outbound_qty_by_unit'))+'**','　High-Discount: **'+quantity(summary.get('high_net_qty_by_unit'))+'**']
    else:lines += ['　Total: **'+display(summary.get('net_outbound_rolls'))+' rolls**','　High-Discount: **'+display(summary.get('high_net_rolls'))+' rolls**']
    lines += ['','**3. Sold by Sales**']
    ordered=sorted(sales_rows,key=lambda r:(number(r.get('net_rolls')) is None,-abs(number(r.get('net_rolls')) or 0),str(r.get('sales_name') or 'Unknown')))
    if ordered:
        lines.append('Sales    Total')
        for row in ordered:
            name=str(row.get('sales_name') or 'Unknown');padding=' '*max(1,12-len(name))
            shown=quantity(row.get('net_quantity_by_unit')) if region.endswith('-HT') else display(row.get('net_rolls'))
            lines.append('　**'+name+'**'+padding+'**'+shown+'**')
    else:lines.append('(none)')
    lines += ['','<font color="comment">Detailed SKU list is in the attachment.</font>']
    if isinstance(detail_semantics,dict) and detail_semantics.get('pool_membership_note'):
        lines += ['', '<font color="comment">'+str(detail_semantics['pool_membership_note'])+'</font>']
    return '\n'.join(lines)

def sales(changes,*,manager=False,region=None):
    from .legacy_price_compat import sales_message
    body=sales_message(changes)
    if manager:
        first,rest=body.split('\n',1)
        return first+'\nRegion: '+str(region or 'Unknown')+'\n'+rest
    return body
