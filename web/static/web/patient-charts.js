document.addEventListener('DOMContentLoaded', () => {
  const source = document.getElementById('chart-data');
  if (!source || typeof Chart === 'undefined') return;
  const data = JSON.parse(source.textContent);
  const dateLabel = value => new Date(value).toLocaleDateString('ru-RU', {day: '2-digit', month: '2-digit', year: 'numeric'});
  Chart.defaults.font.family = 'Golos, system-ui, sans-serif';
  Chart.defaults.color = '#615C6C';
  function draw(id, series, max) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    // Общая временная ось сохраняет порядок дат и несколько записей за день.
    const dates = [...new Set(series.flatMap(s => s.points.map(p => p.x)))].sort((a, b) => new Date(a) - new Date(b));
    new Chart(canvas, {
      type: 'line',
      data: {
        labels: dates,
        datasets: series.map(s => ({
          label: s.label,
          data: dates.map(date => s.points.find(p => p.x === date)?.y ?? null),
          borderColor: s.color, backgroundColor: s.color,
          borderDash: s.dash || [], borderWidth: 2, pointRadius: 3,
          tension: 0, spanGaps: true,
        })),
      },
      options: {
        maintainAspectRatio: false, animation: false,
        plugins: {
          legend: {position: 'bottom', labels: {boxWidth: 12}},
          tooltip: {callbacks: {title: items => new Date(items[0].label).toLocaleString('ru-RU')}},
        },
        scales: {
          y: {min: 0, max, grid: {color: '#E6E1EA'}},
          x: {grid: {display: false}, ticks: {maxTicksLimit: 5, maxRotation: 0, callback: function(value) {return dateLabel(this.getLabelForValue(value));}}},
        },
      },
    });
  }
  draw('ch-scales', [
    {label: 'PHQ-9 (0–27)', points: data.phq9, color: '#705681'},
    {label: 'GAD-7 (0–21)', points: data.gad7, color: '#386D59', dash: [6, 4]},
  ], 27);
  draw('ch-diary', [
    {label: 'Настроение', points: data.mood, color: '#386D59'},
    {label: 'Тревога', points: data.anxiety, color: '#705681', dash: [6, 4]},
  ], 10);
});
