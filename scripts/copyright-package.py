#!/usr/bin/env python3
"""Build the software manual and source materials as a separate ZIP."""
import argparse
import hashlib
import json
import pathlib
import re
import tempfile
import zipfile
from xml.sax.saxutils import escape
from package import source_listing

NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def write_xml(archive, name, content):
    entry=zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0))
    entry.compress_type=zipfile.ZIP_DEFLATED
    archive.writestr(entry, content)


def paragraph(text, style=None, code=False):
    properties = '<w:pPr>' + (f'<w:pStyle w:val="{style}"/>' if style else '')
    properties += '<w:spacing w:after="0" w:line="240" w:lineRule="exact"/>' if code else '<w:spacing w:after="100"/>'
    properties += '</w:pPr>'
    font = 'Consolas' if code else 'SimSun'
    size = 18 if code else (32-int(style[-1])*2 if style else 22)
    run = f'<w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="SimSun"/><w:sz w:val="{size}"/></w:rPr>'
    return '<w:p>' + properties + '<w:r>' + run + '<w:t xml:space="preserve">' + escape(text) + '</w:t></w:r></w:p>'


def document(text, destination, code=False):
    parts = []
    lines = iter(text.splitlines())
    for line in lines:
        if code:
            parts.append(paragraph(line, code=True)); continue
        if line.startswith('|'):
            cells = line.strip('|').split('|')
            if all(re.fullmatch(r'\s*:?-+:?\s*', cell) for cell in cells): continue
            width=9638//len(cells)
            grid='<w:tblGrid>'+''.join(f'<w:gridCol w:w="{width}"/>' for _ in cells)+'</w:tblGrid>'
            row = '<w:tr>' + ''.join(f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>' + paragraph(cell.strip()) + '</w:tc>' for cell in cells) + '</w:tr>'
            parts.append('<w:tbl><w:tblPr><w:tblBorders>' + ''.join(f'<w:{edge} w:val="single" w:sz="4" w:color="B0B0B0"/>' for edge in ['top','left','bottom','right','insideH','insideV']) + '</w:tblBorders></w:tblPr>' + grid + row + '</w:tbl>')
        else:
            heading = re.match(r'^(#{1,3})\s+(.*)', line)
            parts.append(paragraph(heading[2] if heading else line, 'Heading'+str(len(heading[1])) if heading else None))
    section = '<w:sectPr><w:headerReference w:type="default" r:id="rHeader"/><w:footerReference w:type="default" r:id="rFooter"/><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567"/></w:sectPr>'
    doc = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>' + ''.join(parts) + section + '</w:body></w:document>'
    content = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    for name, kind in [('document','document.main'),('styles','styles'),('header1','header'),('footer1','footer')]:
        content += f'<Override PartName="/word/{name}.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml"/>'
    content += '</Types>'
    relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    for identity, kind, target in [('rStyles','styles','styles.xml'),('rHeader','header','header1.xml'),('rFooter','footer','footer1.xml')]:
        relationships += f'<Relationship Id="{identity}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}" Target="{target}"/>'
    styles = f'<w:styles xmlns:w="{NS}">' + ''.join(f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/><w:pPr><w:keepNext/></w:pPr><w:rPr><w:b/><w:sz w:val="{32-level*2}"/></w:rPr></w:style>' for level in range(1,4)) + '</w:styles>'
    footer = f'<w:ftr xmlns:w="{NS}"><w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>第 </w:t></w:r><w:fldSimple w:instr="PAGE"/><w:r><w:t> 页</w:t></w:r></w:p></w:ftr>'
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        write_xml(archive, '[Content_Types].xml', content)
        write_xml(archive, '_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        write_xml(archive, 'word/document.xml', doc)
        write_xml(archive, 'word/_rels/document.xml.rels', relationships + '</Relationships>')
        write_xml(archive, 'word/styles.xml', styles)
        write_xml(archive, 'word/header1.xml', f'<w:hdr xmlns:w="{NS}">' + paragraph('轻量级视频共享与传输软件 V1.0') + '</w:hdr>')
        write_xml(archive, 'word/footer1.xml', footer)


def package(output):
    project = pathlib.Path(__file__).resolve().parents[1]
    output = pathlib.Path(output).absolute(); output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        folder = pathlib.Path(temporary)
        manual = (project / 'docs/copyright-draft.md').read_text()
        (folder / '软件说明书.md').write_text(manual)
        (folder / 'LICENSE.txt').write_text((project / 'LICENSE').read_text())
        document(manual, folder / '软件说明书.docx')
        stats = source_listing(project, folder / '源程序.txt')
        document((folder / '源程序.txt').read_text(), folder / '源程序.docx', code=True)
        (folder / '材料信息表.md').write_text('# 材料信息表\n\n软件名称：轻量级视频共享与传输软件\n\n版本：V1.0\n\n著作权人：【按实际身份填写】\n\n开发完成日期：【按实际事实填写】\n\n首次发表日期与发表状态：【按实际事实填写】\n\n开发方式与权利取得依据：【按实际情况填写】\n')
        (folder / '阅读说明.txt').write_text('本包包含软件说明书和完整项目源码材料，DOCX 可在文字处理软件中排版。\n源程序按实际文件顺序汇编，排除第三方 js-sha256 与测试夹具。\n申报前请核对身份与日期信息，按登记系统要求确认页码和选页。\n项目依赖及许可通知保留在代码仓库中。\n')
        (folder / '材料清单.json').write_text(json.dumps({'software':'轻量级视频共享与传输软件','version':'V1.0','source':stats}, ensure_ascii=False, indent=2)+'\n')
        entries = sorted(folder.iterdir())
        sums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name+'\n' for path in entries)
        (folder / 'SHA256SUMS').write_text(sums)
        fingerprint = hashlib.sha256(sums.encode()).hexdigest()[:12]
        destination = output / f'lanvideo-1.0.0-copyright-{fingerprint}.zip'
        with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(folder.iterdir()):
                entry = zipfile.ZipInfo(path.name, date_time=(1980,1,1,0,0,0)); entry.compress_type=zipfile.ZIP_DEFLATED
                archive.writestr(entry, path.read_bytes())
        return {'archive':str(destination),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'source':stats}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='build/artifacts')
    args=parser.parse_args()
    try: print(json.dumps(package(args.output),ensure_ascii=False))
    except (OSError,ValueError) as error: parser.exit(1, str(error)+'\n')
