// A small event-capable DOM for exercising renderers and lifecycle callbacks.
// Layout and real device integration are separate deployment checks.
export class Element extends EventTarget {
  constructor(tag = 'div') {
    super();
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.style = {};
    this.dataset = {};
    this.className = '';
    this.hidden = false;
    this.checked = false;
    this.disabled = false;
    this.type = '';
    this.value = '';
    this.classList = {
      contains: (name) => this.className.split(' ').includes(name),
      add: (name) => { if (!this.classList.contains(name)) this.className += ` ${name}`; },
      remove: (name) => { this.className = this.className.split(' ').filter((v) => v !== name).join(' '); },
      toggle: (name, on) => on ? this.classList.add(name) : this.classList.remove(name),
    };
  }
  get textContent() { return (this.text || '') + this.children.map((node) => node.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get firstChild() { return this.children[0]; }
  appendChild(node) { node.parent = this; this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = []; this.text = ''; nodes.forEach((node) => this.appendChild(node)); }
  removeChild(node) { this.children = this.children.filter((child) => child !== node); }
  remove() { this.parent?.removeChild(this); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  querySelector(selector) { return findAll(this, (node) => node.classList.contains(selector.slice(1)))[0] || null; }
}

export function findAll(root, match) {
  return [...(match(root) ? [root] : []), ...root.children.flatMap((node) => findAll(node, match))];
}

export function installDom() {
  const nodes = new Map();
  const root = new Element();
  root.hidden = true;
  for (const name of ['sheet-body', 'sheet-title', 'sheet-close', 'sheet-grip']) {
    const node = new Element(); node.className = name; root.appendChild(node);
  }
  nodes.set('sheet', root);
  nodes.set('nav-root', new Element());
  const doc = Object.assign(new EventTarget(), {
    createElement: (tag) => new Element(tag),
    createElementNS: (_ns, tag) => new Element(tag),
    createTextNode: (value) => { const node = new Element('#text'); node.textContent = value; return node; },
    getElementById: (id) => nodes.get(id) || null,
    body: new Element('body'), documentElement: new Element('html'), visibilityState: 'visible',
  });
  const storage = new Map([['nicanav.safetyNotice', '1']]);
  const storageApi = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  Object.assign(globalThis, {
    Node: Element, document: doc, window: { setTimeout, clearTimeout },
    localStorage: storageApi, sessionStorage: storageApi,
  });
  return { nodes, doc, storage };
}

export const flush = () => new Promise((resolve) => setImmediate(resolve));

export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
