// Read only the rendered slide and lifecycle UI. Never capture a whole browser page.
() => {
  const visible = (element) => element && element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== 'hidden';
  const classroom = [...document.querySelectorAll('.classroom')].some(visible);
  const join = [...document.querySelectorAll('input[placeholder="Class code"]')].some(visible);
  const notices = [...document.querySelectorAll('[role="dialog"], [role="alert"], .MuiDialog-root, .MuiSnackbar-root')]
    .filter(visible).map(element => element.innerText).join('\n');
  // Outside a classroom, the landing/end screen is also lifecycle UI.
  const statusText = classroom ? notices : document.body.innerText;
  const ended = /(?:class|session)\s+(?:has\s+)?(?:ended|closed)|presenter\s+(?:has\s+)?(?:ended|closed|left)|removed from (?:the )?class/i.test(statusText);
  const disconnected = !navigator.onLine || /connection (?:lost|failed)|reconnecting|disconnected|unable to connect/i.test(notices);
  const state = { classroom, join, ended, disconnected, slide: null };
  // A quiz/profile/connection dialog can cover the slide. Never save that UI.
  if ([...document.querySelectorAll('[role="dialog"], .MuiDialog-root')].some(visible)) return state;
  const image = document.querySelector('.presenter_img:not(.audienceimage):not(.loading_page) img[src]');
  if (!classroom || !visible(image) || !image.complete || !image.naturalWidth || !image.naturalHeight) return state;
  const style = getComputedStyle(image), rect = image.getBoundingClientRect();
  if (Number(style.opacity) === 0) return state;
  const inset = side => (parseFloat(style[`border${side}Width`]) || 0) + (parseFloat(style[`padding${side}`]) || 0);
  let x = rect.left + scrollX + inset('Left'), y = rect.top + scrollY + inset('Top');
  let width = rect.width - inset('Left') - inset('Right');
  let height = rect.height - inset('Top') - inset('Bottom');
  if (['contain', 'scale-down', 'none'].includes(style.objectFit)) {
    let scale = Math.min(width / image.naturalWidth, height / image.naturalHeight);
    if (style.objectFit === 'none') scale = 1;
    if (style.objectFit === 'scale-down') scale = Math.min(1, scale);
    const fittedWidth = Math.min(width, image.naturalWidth * scale);
    const fittedHeight = Math.min(height, image.naturalHeight * scale);
    const [horizontal = '50%', vertical = '50%'] = style.objectPosition.split(' ');
    const offset = (value, space) => value.endsWith('%') ? space * parseFloat(value) / 100 : parseFloat(value) || 0;
    x += offset(horizontal, width - fittedWidth);
    y += offset(vertical, height - fittedHeight);
    width = fittedWidth; height = fittedHeight;
  }
  if (x < 0 || y < 0 || width < 1 || height < 1) return state;
  state.slide = {
    src: image.currentSrc || image.src,
    clip: { x, y, width, height, scale: 1 },
    revision: window.__classpointSlideRevision || 0
  };
  return state;
}
