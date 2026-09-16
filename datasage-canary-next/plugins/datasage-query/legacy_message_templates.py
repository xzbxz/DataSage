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
    return 'Unknown' if value is None else format(value.normalize(),'f')
def quantity(values):
    return ' | '.join(str(k)+': '+display(v) for k,v in sorted(values.items())) if isinstance(values,dict) and values else 'Unknown'

def report(region,period,summary,sales_rows,monthly=False):
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
        year,week=period.split('-W');start=date.fromisocalendar(int(year),int(week),1);end=start+timedelta(days=5)
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
    ordered=sorted(sales_rows,key=lambda r:(number(r.get('net_rolls')) is None,-abs(number(r.get('net_rolls')) or 0),str(r.get('sales_name','Unknown'))))
    if ordered:
        lines.append('Sales    Total')
        for row in ordered:
            name=str(row.get('sales_name') or 'Unknown');padding=' '*max(1,12-len(name))
            shown=quantity(row.get('net_quantity_by_unit')) if region.endswith('-HT') else display(row.get('net_rolls'))
            lines.append('　**'+name+'**'+padding+'**'+shown+'**')
    else:lines.append('(none)')
    lines += ['','<font color="comment">Detailed SKU list is in the attachment.</font>']
    return '\n'.join(lines)

def sales(changes,*,manager=False,region=None):
    grouped={}
    for row in changes:grouped.setdefault(row.get('goods_no') or row.get('goods_name') or 'Unknown',[]).append(row)
    lines=['Ready Product Price Adjustment']
    if manager:lines.append('Region: '+str(region or 'Unknown'))
    lines += ['',str(len(grouped))+' product(s) price changed:']
    for product,rows in grouped.items():
        for row in rows:
            old=number(row.get('old_ddp_price'));new=number(row.get('new_ddp_price'))
            delta='Unknown' if old is None or new is None else ('+' if new-old>=0 else '')+format(new-old,'.2f')
            lines.append(f'- {product} ({row.get("customer_grade") or "-"}/{row.get("color_label") or "-"}): {display(old)} -> {display(new)} ({delta}) {row.get("currency_no") or ""}')
    return '\n'.join(lines)
