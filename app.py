from flask import Flask, render_template, request, send_file, jsonify
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from io import BytesIO

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/export_excel', methods=['POST'])
def export_excel():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    wb = Workbook()
    ws = wb.active
    ws.title = "Рассадка"

    header_font = Font(name='Calibri', bold=True, size=12, color='FFFFFF')
    header_fill = PatternFill(start_color='2C3E50', end_color='2C3E50', fill_type='solid')
    thin = Side(border_style='thin', color='CCCCCC')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center')

    for col_idx, header in enumerate(['Ряд', 'Место', 'Группа'], 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border
    ws.row_dimensions[1].height = 22

    def sort_key(item):
        try:
            r = int(item.get('row', 9999))
        except (ValueError, TypeError):
            r = 9999
        try:
            s = float(item.get('seat', 9999))
        except (ValueError, TypeError):
            s = 9999.0
        return (r, s)

    for row_idx, item in enumerate(sorted(data, key=sort_key), 2):
        color_hex = item.get('color', '#FFFFFF').lstrip('#')
        ws.cell(row=row_idx, column=1, value=item.get('row', '')).alignment = center
        ws.cell(row=row_idx, column=2, value=item.get('seat', '')).alignment = center
        group_cell = ws.cell(row=row_idx, column=3, value=item.get('group', ''))
        group_cell.alignment = center
        try:
            group_cell.fill = PatternFill(start_color=color_hex, end_color=color_hex, fill_type='solid')
            r, g, b = int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
            group_cell.font = Font(name='Calibri', size=11, color='000000' if lum > 0.5 else 'FFFFFF')
        except Exception:
            pass
        for col in range(1, 4):
            ws.cell(row=row_idx, column=col).border = border
        ws.row_dimensions[row_idx].height = 18

    ws.column_dimensions['A'].width = 10
    ws.column_dimensions['B'].width = 10
    ws.column_dimensions['C'].width = 30

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name='seating.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


if __name__ == '__main__':
    app.run(debug=True)