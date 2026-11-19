"""Headless charts rendered in memory, serialized to bound server memory usage."""

from io import BytesIO
import os
from pathlib import Path
import tempfile
import threading
import textwrap

_RENDER_LOCK = threading.Lock()
COLORS = ['#538679', '#a1b9c8', '#ca9b76', '#af858c']


def render_dashboard(data):
    with _RENDER_LOCK:
        # Font cache must be writable in the restricted server service.
        os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'ledgerly-matplotlib'))
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.ticker import MaxNLocator
        from matplotlib.text import Text
        figure = Figure(figsize=(12, 13), dpi=120, facecolor='#f4f5f2')
        FigureCanvasAgg(figure)
        axes = figure.subplots(3, 2)
        stats = data['stats']
        period = 'весь час' if data['period'] == 'all' else data['period']
        figure.suptitle('Ledgerly · ' + period, fontsize=23, color='#263c39', y=.97)
        money = lambda cents: f'{int(cents) / 100:,.2f}'.replace(',', ' ')
        figure.text(.5, .928, 'Оборот %s грн   ·   Прибуток %s грн   ·   Продажів %s' % (
            money(stats['turnover_cents']), money(stats['profit_cents']), stats['sales_count']),
            ha='center', fontsize=11, color='#52635f')
        for ax in axes.flat:
            ax.set_facecolor('#ffffff')
            ax.spines[['top', 'right']].set_visible(False)
            ax.spines[['left', 'bottom']].set_color('#d8dfdb')
            ax.tick_params(colors='#52635f', labelsize=9)
            ax.set_axisbelow(True)
        def empty(ax):
            ax.text(.5, .5, 'Поки немає даних', ha='center', va='center', transform=ax.transAxes, color='#74847e')
            ax.set_xticks([]); ax.set_yticks([])
        def bars(ax, labels, values, colors=COLORS, horizontal=False):
            if not values or not any(values):
                empty(ax); return
            positions = list(range(len(values)))
            draw = ax.barh if horizontal else ax.bar
            rects = draw(positions, values, color=colors)
            if horizontal:
                ax.set_yticks(positions, labels); ax.invert_yaxis()
            else:
                ax.set_xticks(positions, labels)
            ax.bar_label(rects, fmt=lambda v: f'{v:,.0f}'.replace(',', ' '), padding=4, fontsize=9)
            ax.margins(x=.18 if horizontal else .12, y=.2)
        timeline = data['timeline']
        ax = axes[0, 0]
        ax.set_title('Оборот і прибуток · грн', loc='left', fontsize=13, pad=16)
        if timeline:
            x = list(range(len(timeline)))
            labels = [row['bucket'][5:] if data['period'] != 'all' else row['bucket'] for row in timeline]
            ax.plot(x, [int(r['turnover_cents']) / 100 for r in timeline], color=COLORS[0], marker='o', label='Оборот')
            ax.plot(x, [int(r['profit_cents']) / 100 for r in timeline], color=COLORS[2], marker='o', label='Прибуток')
            step = max(1, (len(labels) + 5) // 6)
            ax.set_xticks(x[::step], labels[::step], rotation=25)
            ax.legend(frameon=False, fontsize=9); ax.grid(axis='y', alpha=.15)
        else:
            empty(ax)
        ax = axes[0, 1]
        ax.set_title('Фінансовий підсумок · грн', loc='left', fontsize=13, pad=16)
        bars(ax, ['Оборот', 'Витрати*', 'Прибуток*'], [stats[k] / 100 for k in ('turnover_cents','expenses_cents','profit_cents')])
        ax = axes[1, 0]
        ax.set_title('Продажі та відмови · замовлення', loc='left', fontsize=13, pad=16)
        bars(ax, ['Продажі', 'Відмови'], [stats['sales_count'], data['refusals']], [COLORS[0], COLORS[3]])
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax = axes[1, 1]
        ax.set_title('Продажі за способом оформлення', loc='left', fontsize=13, pad=16)
        labels = {'OLX':'OLX', 'COD':'Післяплата', 'FOP':'ФОП', 'OFFLINE':'Офлайн', 'FULL':'Повна оплата'}
        bars(ax, [labels.get(r['name'], r['name']) for r in data['channels']], [int(r['amount']) for r in data['channels']])
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax = axes[2, 0]
        ax.set_title('Топ товарів за оборотом · грн', loc='left', fontsize=13, pad=16)
        bars(ax, ['\n'.join(textwrap.wrap(str(r['name'])[:80], 22)) for r in data['top_products']],
             [int(r['turnover_cents']) / 100 for r in data['top_products']], COLORS[0], horizontal=True)
        ax = axes[2, 1]
        ax.set_title('Склад зараз · штук', loc='left', fontsize=13, pad=16)
        bars(ax, ['Доступно', 'Резерв', 'У дорозі'], [data['stock'][k] for k in ('available','awaiting_shipment','in_transit')])
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        footer = '* Витрати й прибуток — лише продажі з відомою собівартістю. Без розрахунку: %s.\n' % stats['cost_pending_count']
        footer += 'Фінанси враховують очищення аналітики; відмови — повну історію за вибраний період. Склад — на цей момент.'
        if data['period'] == 'all':
            footer += '\nЛінія показує останні 24 місяці з продажами; підсумки — за весь час.'
        figure.text(.5, .025, footer, ha='center', va='bottom', fontsize=9, color='#52635f')
        figure.subplots_adjust(left=.16, right=.96, top=.87, bottom=.12, hspace=.6, wspace=.45)
        output = BytesIO()
        try:
            # Product names are user data; dollar signs must stay literal text.
            for label in figure.findobj(Text):
                label.set_parse_math(False)
            figure.savefig(output, format='png', dpi=120)
        finally:
            figure.clear()
        output.seek(0)
        output.name = 'ledgerly-charts.png'
        return output
