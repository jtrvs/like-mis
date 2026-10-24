document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-progress-form]').forEach(form => {
    const names = [...new Set([...form.querySelectorAll('input[type="radio"][required]')].map(input => input.name))];
    const progress = form.querySelector('progress');
    const label = form.querySelector('[data-progress-text]');
    const update = () => {
      const answered = names.filter(name => [...form.querySelectorAll('input[type="radio"]')].some(input => input.name === name && input.checked)).length;
      progress.value = answered;
      label.textContent = `${form.classList.contains('scale-form') ? 'Отвечено' : 'Заполнено обязательных полей'}: ${answered} из ${names.length}`;
    };
    form.addEventListener('change', update);
    update();
  });
});
