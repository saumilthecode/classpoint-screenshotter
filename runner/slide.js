// Locate the slide crop and read session status from the rendered page.
() => {
  const visible = element => element.getClientRects().length > 0 &&
    element.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const classrooms = [...document.querySelectorAll('.classroom')].filter(visible);
  const classroom = classrooms.length > 0;
  const join = [...document.querySelectorAll('input[placeholder="Class code"]')].some(visible);
  const notices = [...document.querySelectorAll('[role="dialog"], [role="alert"], .MuiDialog-root, .MuiSnackbar-root')]
    .filter(visible).map(element => element.innerText).join('\n');
  // Outside a classroom, end notices can appear directly on the page.
  const statusText = classroom ? notices : (document.body?.innerText || '');
  const ended = /(?:class|session)\s+(?:has\s+)?(?:ended|closed)|presenter\s+(?:has\s+)?(?:ended|closed|left)|removed from (?:the )?class/i.test(statusText);
  const disconnected = !navigator.onLine || /connection (?:lost|failed)|reconnecting|disconnected|unable to connect/i.test(notices);
  const state = { classroom, join, ended, disconnected, slide: null };
  // Skip slides covered by a dialog.
  if ([...document.querySelectorAll('[role="dialog"], .MuiDialog-root')].some(visible)) return state;
  const insideClippingContainers = (image, rect) => {
    let { left, right, top, bottom } = rect;
    for (let parent = image.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      const { overflowX, overflowY } = getComputedStyle(parent);
      if (overflowX === 'visible' && overflowY === 'visible') continue;
      const bounds = parent.getBoundingClientRect();
      if (overflowX !== 'visible') {
        left = Math.max(left, bounds.left);
        right = Math.min(right, bounds.right);
      }
      if (overflowY !== 'visible') {
        top = Math.max(top, bounds.top);
        bottom = Math.min(bottom, bounds.bottom);
      }
      if (right <= left || bottom <= top) return false;
    }
    return true;
  };
  const candidates = [...document.querySelectorAll('.presenter_img img[src], .presenter_img img[srcset]')].filter(image => {
    const presenter = image.closest('.presenter_img');
    return classrooms.includes(presenter.closest('.classroom')) &&
      !presenter.closest('.audienceimage, .loading_page') && visible(image);
  }).map(image => {
    const rect = image.getBoundingClientRect();
    return { image, rect, area: rect.width * rect.height };
  }).filter(({ image, rect, area }) => area > 0 && insideClippingContainers(image, rect))
    .sort((a, b) => b.area - a.area);
  // Wait when a handoff leaves two slide-sized images visible. Small illustrations
  // or icons inside the presenter do not prevent finding its main image.
  const [primary, next] = candidates;
  if (!primary || (next && next.area * 4 >= primary.area)) return state;
  const { image, rect } = primary;
  if (!image.complete || !image.naturalWidth || !image.naturalHeight) return state;
  // Wait through finite page transitions that fade or move the presenter image.
  for (let layer = image; layer; layer = layer.parentElement) {
    const moving = layer.getAnimations().some(animation => animation.playState === 'running' &&
      Number.isFinite(animation.effect?.getComputedTiming().endTime) &&
      animation.effect.getKeyframes().some(frame =>
        ['opacity', 'transform', 'scale', 'translate', 'width', 'height', 'clipPath'].some(property => property in frame)));
    if (moving) return state;
    if (layer.matches('.presenter_img')) break;
  }
  const style = getComputedStyle(image);
  const inset = side => (parseFloat(style[`border${side}Width`]) || 0) + (parseFloat(style[`padding${side}`]) || 0);
  let x = rect.left + scrollX + inset('Left'), y = rect.top + scrollY + inset('Top');
  let width = rect.width - inset('Left') - inset('Right');
  let height = rect.height - inset('Top') - inset('Bottom');
  if (width < 1 || height < 1) return state;
  const fitX = width / image.naturalWidth, fitY = height / image.naturalHeight;
  const contain = Math.min(fitX, fitY);
  const objectScale = { contain, cover: Math.max(fitX, fitY), 'scale-down': Math.min(1, contain), none: 1 }[style.objectFit];
  const renderedWidth = objectScale === undefined ? width : image.naturalWidth * objectScale;
  const renderedHeight = objectScale === undefined ? height : image.naturalHeight * objectScale;
  if (renderedWidth !== width || renderedHeight !== height) {
    const [horizontal, vertical] = style.objectPosition.match(/calc\([^)]*\)|\S+/g) || [];
    // Chromium resolves the keywords and right/bottom offsets to percentages or calc().
    const offset = (value, space) => {
      if (!value) return space / 2;
      if (value.startsWith('calc(')) {
        const match = value.match(/^calc\((-?[\d.]+)% ([+-]) ([\d.]+)px\)$/);
        return match ? space * Number(match[1]) / 100 + (match[2] === '+' ? 1 : -1) * Number(match[3]) : space / 2;
      }
      return value.endsWith('%') ? space * parseFloat(value) / 100 : parseFloat(value) || 0;
    };
    const left = offset(horizontal, width - renderedWidth);
    const top = offset(vertical, height - renderedHeight);
    width = Math.min(width, left + renderedWidth) - Math.max(0, left);
    height = Math.min(height, top + renderedHeight) - Math.max(0, top);
    x += Math.max(0, left);
    y += Math.max(0, top);
  }
  if (x < 0 || y < 0 || width < 1 || height < 1) return state;
  state.slide = {
    src: image.currentSrc || image.src,
    clip: { x, y, width, height, scale: 1 },
    source_size: [image.naturalWidth, image.naturalHeight],
    source_density: Math.min(image.naturalWidth / renderedWidth, image.naturalHeight / renderedHeight),
    revision: window.__classpointSlideRevision || 0
  };
  return state;
}
