(() => {
  const body = document.body;
  if (!body.classList.contains('app-page')) return;

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const cards = [...document.querySelectorAll('.card')];
  cards.forEach((card, index) => {
    card.classList.add('ui-premium-card', 'ui-reveal');
    card.style.setProperty('--ui-delay', `${Math.min(index * 45, 270)}ms`);

    card.addEventListener('pointermove', (event) => {
      const rect = card.getBoundingClientRect();
      card.style.setProperty('--ui-x', `${event.clientX - rect.left}px`);
      card.style.setProperty('--ui-y', `${event.clientY - rect.top}px`);
    });
  });

  const revealTargets = [
    ...cards,
    ...document.querySelectorAll('.main-content > .d-flex:first-of-type, .main-content > h2'),
  ];

  revealTargets.forEach((target, index) => {
    target.classList.add('ui-reveal');
    if (!target.style.getPropertyValue('--ui-delay')) {
      target.style.setProperty('--ui-delay', `${Math.min(index * 40, 220)}ms`);
    }
  });

  requestAnimationFrame(() => {
    requestAnimationFrame(() => revealTargets.forEach((target) => target.classList.add('ui-visible')));
  });

  document.querySelectorAll('.btn').forEach((button) => {
    button.addEventListener('pointerdown', (event) => {
      if (reduceMotion || button.disabled) return;
      const rect = button.getBoundingClientRect();
      const ripple = document.createElement('span');
      ripple.className = 'ui-ripple';
      ripple.style.left = `${event.clientX - rect.left}px`;
      ripple.style.top = `${event.clientY - rect.top}px`;
      button.appendChild(ripple);
      window.setTimeout(() => ripple.remove(), 650);
    });
  });

})();
