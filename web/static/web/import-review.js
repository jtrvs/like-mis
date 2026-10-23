document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-import-medication]').forEach(row => {
    const choice = row.querySelector('input[type=checkbox]');
    const dates = row.querySelectorAll('input[type=date]');
    const update = () => dates.forEach(input => {
      input.disabled = !choice.checked;
      input.required = choice.checked;
    });
    choice.addEventListener('change', update);
    update();
  });
});
