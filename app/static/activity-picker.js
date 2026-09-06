document.querySelectorAll('[data-activity-picker]').forEach((picker) => {
  const available = picker.querySelector('[data-activity-available]');
  const selected = picker.querySelector('[data-activity-selected]');
  const empty = picker.querySelector('[data-activity-empty]');
  const sync = () => {
    picker.querySelectorAll('input[name="activity_types"]').forEach((input) => input.remove());
    selected.querySelectorAll('[data-activity-value]').forEach((item) => {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'activity_types'; input.value = item.dataset.activityValue;
      picker.appendChild(input);
    });
    empty.hidden = selected.children.length > 0;
  };
  const move = (item, target) => {
    const adding = target === selected;
    item.dataset.move = adding ? 'remove' : 'add';
    const action = item.querySelector('span');
    if (action) action.textContent = adding ? 'Remove' : 'Add';
    target.appendChild(item); sync();
  };
  picker.addEventListener('click', (event) => {
    if (event.target.closest('[data-clear-selected]')) {
      [...selected.querySelectorAll('[data-activity-value]')].forEach((item) => move(item, available));
      return;
    }
    const button = event.target.closest('[data-move]');
    if (!button) return;
    const item = button.closest('[data-activity-value]');
    if (item) move(item, button.dataset.move === 'add' ? selected : available);
  });
  sync();
});
