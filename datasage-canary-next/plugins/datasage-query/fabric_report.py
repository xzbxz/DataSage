"""Actual source-backed fabric workbook and charts; no responsibility inference."""
from pathlib import Path
from decimal import Decimal
from xml.etree import ElementTree as ET
import zipfile,math
from .legacy_xlsx import gen_workbook_xlsx

FIELDS={'metric_value':'源记录数','fabric_rolls':'完整卷数','fabric_known_rolls':'已知卷数','fabric_tagged_rolls':'完整标签卷数','fabric_known_tagged_rolls':'已知标签卷数','fabric_ddp_rmb':'完整DDP人民币','fabric_known_ddp_rmb':'已知DDP人民币','fabric_tagged_ddp_rmb':'完整标签DDP人民币','fabric_known_tagged_ddp_rmb':'已知标签DDP人民币','fabric_sales_rmb':'完整销售人民币','fabric_known_sales_rmb':'已知销售人民币','fabric_tagged_ddp_gap_rmb':'完整标签DDP差额','fabric_known_tagged_ddp_gap_rmb':'已知标签DDP差额','fabric_tag_rate':'自身标签发生率','fabric_tag_contribution':'标签卷数贡献率','fabric_missing_rolls':'缺卷数行','fabric_unknown_tags':'未知标签行','fabric_formation_pending':'形成时期待核验行','fabric_multiple_roots':'多根血缘行','fabric_scope_unknown':'范围待核验行','fabric_read_at':'读取时点','fabric_etl_min':'最早ETL','fabric_etl_max':'最晚ETL'}

def n(value):
    if value is None:return None
    try:
        x=Decimal(str(value));return x if x.is_finite() else None
    except Exception:return None

def observations(doc):
    if 'observations' in doc:return doc['observations']
    result=[]
    for i,packet in enumerate(doc.get('query_packets',[])):
        if packet.get('status')!='success':continue
        for item in packet.get('results',[]):
            result.append({'name':['出库总览','出库渠道','出库形成时期','库存总览','库存渠道','库存形成时期'][i] if i<6 else str(i),'rows':[{'group':' / '.join(str(d.get('value')) for d in row.get('dimensions',[])) or '总体','facts':row.get('facts',{})} for row in item.get('rows',[])],'truncated':item.get('truncated',False),'source':'existing_governed_query','time':item.get('applied_time_range')})
    return result

def chart(path,title,rows,series):
    from PIL import Image,ImageDraw,ImageFont
    font_path=Path('C:/Windows/Fonts/msyh.ttc');font=ImageFont.truetype(str(font_path),15);bold=ImageFont.truetype(str(font_path),19)
    height=115+len(rows)*65;im=Image.new('RGB',(1000,max(180,height)),'white');d=ImageDraw.Draw(im)
    d.text((20,15),title,font=bold,fill='#172d46')
    colors=['#24679c','#bd7433'];values=[float(n(r['facts'].get(f)) or 0) for r in rows for f,_ in series]
    rate=all(f in ('fabric_tag_rate','fabric_tag_contribution') for f,_ in series);maximum=max(values+[1])
    for k,(_,label) in enumerate(series):d.rectangle((20+k*270,51,35+k*270,66),fill=colors[k]);d.text((43+k*270,47),label,font=font,fill='#172d46')
    for i,row in enumerate(rows):
        y=91+i*65;d.text((20,y),str(row['group']),font=font,fill='#172d46')
        for j,(field,_) in enumerate(series):
            value=n(row['facts'].get(field));yy=y+j*25
            if value is None:d.text((230,yy),'未知，不填零',font=font,fill='#855c38');continue
            if value<0:d.text((230,yy),'负值需核对：'+str(value),font=font,fill='#855c38');continue
            width=int(float(value)/maximum*610)
            if width>0:d.rectangle((230,yy,230+width,yy+17),fill=colors[j])
            d.text((850,yy-2),f'{float(value)*100:.1f}%' if rate else str(value),font=font,fill='#172d46')
    im.save(path,'PNG')

def embed_charts(book,images,start_row):
    # Small fixed drawing attachment, reusing the existing workbook emitter.
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main';rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships';pkg='http://schemas.openxmlformats.org/package/2006/relationships';draw='http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing';a='http://schemas.openxmlformats.org/drawingml/2006/main'
    ET.register_namespace('',ns);ET.register_namespace('r',rel);ET.register_namespace('xdr',draw);ET.register_namespace('a',a)
    with zipfile.ZipFile(book) as z:parts={name:z.read(name) for name in z.namelist()}
    sheet=ET.fromstring(parts['xl/worksheets/sheet1.xml']);ET.SubElement(sheet,'{'+ns+'}drawing',{'{'+rel+'}id':'rIdCharts'});parts['xl/worksheets/sheet1.xml']=ET.tostring(sheet,encoding='utf-8',xml_declaration=True)
    relations=ET.Element('Relationships',xmlns=pkg);ET.SubElement(relations,'Relationship',Id='rIdCharts',Type=rel+'/drawing',Target='../drawings/drawing1.xml');parts['xl/worksheets/_rels/sheet1.xml.rels']=ET.tostring(relations,encoding='utf-8',xml_declaration=True)
    drawing=ET.Element('{'+draw+'}wsDr');rels=ET.Element('Relationships',xmlns=pkg)
    from PIL import Image
    for i,path in enumerate(images,1):
        with Image.open(path) as im:w,h=im.size
        anchor=ET.SubElement(drawing,'{'+draw+'}twoCellAnchor',editAs='oneCell');frm=ET.SubElement(anchor,'{'+draw+'}from')
        for tag,value in [('col',0),('colOff',0),('row',start_row),('rowOff',0)]:ET.SubElement(frm,'{'+draw+'}'+tag).text=str(value)
        to=ET.SubElement(anchor,'{'+draw+'}to')
        for tag,value in [('col',10),('colOff',0),('row',start_row+math.ceil(h/20)),('rowOff',0)]:ET.SubElement(to,'{'+draw+'}'+tag).text=str(value)
        pic=ET.SubElement(anchor,'{'+draw+'}pic');nv=ET.SubElement(pic,'{'+draw+'}nvPicPr');ET.SubElement(nv,'{'+draw+'}cNvPr',id=str(i),name='Source chart '+str(i));ET.SubElement(nv,'{'+draw+'}cNvPicPr')
        fill=ET.SubElement(pic,'{'+draw+'}blipFill');ET.SubElement(fill,'{'+a+'}blip',{'{'+rel+'}embed':'rId'+str(i)});stretch=ET.SubElement(fill,'{'+a+'}stretch');ET.SubElement(stretch,'{'+a+'}fillRect');sp=ET.SubElement(pic,'{'+draw+'}spPr');xf=ET.SubElement(sp,'{'+a+'}xfrm');ET.SubElement(xf,'{'+a+'}off',x='0',y='0');ET.SubElement(xf,'{'+a+'}ext',cx=str(w*9525),cy=str(h*9525));geo=ET.SubElement(sp,'{'+a+'}prstGeom',prst='rect');ET.SubElement(geo,'{'+a+'}avLst');ET.SubElement(anchor,'{'+draw+'}clientData')
        ET.SubElement(rels,'Relationship',Id='rId'+str(i),Type=rel+'/image',Target='../media/chart'+str(i)+'.png');parts['xl/media/chart'+str(i)+'.png']=Path(path).read_bytes();start_row+=math.ceil(h/20)+2
    parts['xl/drawings/drawing1.xml']=ET.tostring(drawing,encoding='utf-8',xml_declaration=True);parts['xl/drawings/_rels/drawing1.xml.rels']=ET.tostring(rels,encoding='utf-8',xml_declaration=True)
    types=ET.fromstring(parts['[Content_Types].xml']);t='http://schemas.openxmlformats.org/package/2006/content-types';ET.register_namespace('',t);ET.SubElement(types,'{'+t+'}Default',Extension='png',ContentType='image/png');ET.SubElement(types,'{'+t+'}Override',PartName='/xl/drawings/drawing1.xml',ContentType='application/vnd.openxmlformats-officedocument.drawing+xml');parts['[Content_Types].xml']=ET.tostring(types,encoding='utf-8',xml_declaration=True)
    temp=book.with_suffix('.tmp')
    with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
        for name,value in parts.items():z.writestr(name,value)
    temp.replace(book)

def export_report(doc,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);data=observations(doc);sheets=[];overview=[];overview_formats={};images=[]
    for item in data:
        for row in item['rows']:
            f=row['facts']
            if item['name'].endswith(('总览','overall')):
                for field,label in FIELDS.items():
                    if field in f:
                        overview.append([item['name'],label,n(f[field]) if n(f[field]) is not None else str(f[field]) if f[field] is not None else '未知'])
                        if field in ('fabric_tag_rate','fabric_tag_contribution'):overview_formats[(len(overview),2)]='percent'
        fields=[f for f in FIELDS if any(f in r['facts'] for r in item['rows'])]
        headers=['分组']+[FIELDS[f] for f in fields]
        rows=[[r['group']]+[n(r['facts'].get(f)) if n(r['facts'].get(f)) is not None else str(r['facts'][f]) if r['facts'].get(f) is not None else '未知' for f in fields] for r in item['rows']]
        formats={i+1:'percent' for i,f in enumerate(fields) if f in ('fabric_tag_rate','fabric_tag_contribution')}
        sheets.append((item['name'],headers,rows,formats))
        if '渠道' in item['name'] or 'channels' in item['name']:
            for label,series in [('发生率与贡献率',[('fabric_tag_rate','渠道自身发生率'),('fabric_tag_contribution','标签问题贡献率')]),('标签卷数',[('fabric_known_tagged_rolls','源标签卷数已知部分')])]:
                observed=item['rows'][0]['facts'].get('fabric_read_at') if item['rows'] else None
                path=out/f'chart-{len(images)+1}.png';chart(path,item['name']+' '+(str(observed)[:10] if observed else '时点见来源')+' · '+label,item['rows'],series);images.append(path)
    overview.append(['状态','生成范围',doc.get('status','saved_observation')])
    overview.append(['边界','来源','各表保留原读取/ETL时间；DDP差额不是损失，源归一渠道不是责任归因'])
    sheets.insert(0,('管理层总览',['观察','指标','值'],overview,overview_formats))
    sheets.append(('未验证事项',['项目','状态'],[['正式责任归因','缺生产ETL/责任证据，不编造名单'],['品质/重复退货','生产规则未齐'],['完整递归血缘','未独立核验'],['截断与未知','各分项受原查询证据限制；图中未知不填零']]))
    path=out/'Fabric_Source_Report.xlsx';gen_workbook_xlsx(sheets,path,borders=True,landscape=True)
    if images:embed_charts(path,images,len(overview)+3)
    return [str(path),*[str(p) for p in images]]
