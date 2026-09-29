// Firebase bootstrap for the HOSTED build of the news page.
//
// The page itself (index.html) is identical in both builds; this module only
// supplies the data. It signs the user in with Google, then reads the news
// collection from Firestore. Firestore rather than a public news.json is the
// point: an auth-gated page over an unprotected JSON file hides only the UI,
// not the archive.
//
// Bump FIREBASE_VERSION if you like; it is pinned so the CDN path is stable.
// NOTE: a static `import` specifier must be a STRING LITERAL - it cannot be a
// template literal, even though dynamic import() can. Hence the literal below.
const FIREBASE_VERSION = "10.12.2";
const CDN = `https://www.gstatic.com/firebasejs/${FIREBASE_VERSION}`;

import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";

const cfg = window.__GL_FIREBASE_CONFIG__;
const listEl = () => document.getElementById('list');
const statsEl = () => document.getElementById('stats');

function say(msg) {
  statsEl().textContent = msg;
}

function gate(html) {
  listEl().innerHTML = '<div id="signin">' + html + '</div>';
}

if (!cfg || !cfg.apiKey) {
  say('Firebase config missing - this build was not deployed correctly.');
  gate('No Firebase configuration was found in this page.');
} else {
  const app = initializeApp(cfg);

  // Loaded lazily so a page that only ever shows the sign-in gate does not pay
  // for the auth + firestore bundles.
  const [{ getAuth, GoogleAuthProvider, signInWithPopup, signInWithRedirect,
           getRedirectResult, signOut, onAuthStateChanged },
         { getFirestore, collection, getDocs, doc, getDoc }] = await Promise.all([
    import(`${CDN}/firebase-auth.js`),
    import(`${CDN}/firebase-firestore.js`),
  ]);

  const auth = getAuth(app);
  const db = getFirestore(app);
  const CACHE_KEY = 'gl_cache_v1';

  // Complete a sign-in that came back via redirect. Must run on every load:
  // the redirect flow leaves the result here, not in the promise that started it.
  getRedirectResult(auth).catch((e) => {
    // 'auth/no-auth-event' is normal when simply loading the page.
    if (e && e.code && e.code !== 'auth/no-auth-event') {
      say('Sign-in failed: ' + (e.code || e.message));
      const hint = document.getElementById('gl-hint');
      if (hint) hint.textContent = describeAuthError(e);
    }
  });

  /**
   * Sign in, preferring the popup but falling back to a full-page redirect.
   *
   * The popup flow needs the popup and this page to share storage so the result
   * can be handed back. Browsers that partition storage or block third-party
   * cookies (Edge/Chrome with strict tracking prevention, Brave, Safari) break
   * that handshake: the login SUCCEEDS inside the popup, the result never
   * reaches this page, and Firebase surfaces it as
   * `auth/popup-closed-by-user` - which reads like the user closed the window,
   * so it looks like a browser bug rather than a storage-policy one.
   *
   * The redirect flow has no such dependency, so it is the fallback.
   */
  function wireSignIn(auth, GoogleAuthProvider, signInWithPopup, signInWithRedirect) {
    const btn = document.getElementById('gl-signin');
    const hint = document.getElementById('gl-hint');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const provider = new GoogleAuthProvider();
      provider.setCustomParameters({ prompt: 'select_account' });
      try {
        await signInWithPopup(auth, provider);
        // onAuthStateChanged takes it from here.
      } catch (e) {
        if (hint) hint.textContent = describeAuthError(e);
        say('Popup sign-in failed (' + (e.code || 'error') + ') - trying redirect...');
        try {
          await signInWithRedirect(auth, provider);
        } catch (e2) {
          btn.disabled = false;
          say('Sign-in failed: ' + (e2.code || e2.message));
          if (hint) hint.textContent = describeAuthError(e2);
        }
      }
    });
  }

  /** Turn an auth error code into something worth reading. */
  function describeAuthError(e) {
    const code = (e && e.code) || '';
    if (code === 'auth/popup-closed-by-user' || code === 'auth/cancelled-popup-request') {
      return 'The popup could not hand the result back to this page, which usually '
        + 'means your browser is blocking third-party cookies or partitioning '
        + 'storage for this site. The redirect sign-in above avoids that. If it '
        + 'still fails, allow cookies for keen-wavelet-275120.web.app and '
        + 'keen-wavelet-275120.firebaseapp.com and try again.';
    }
    if (code === 'auth/unauthorized-domain') {
      return 'This domain is not in the Firebase authorised-domains list. Add it '
        + 'under Authentication then Settings then Authorized domains.';
    }
    if (code === 'auth/network-request-failed') {
      return 'A network request failed - check the connection and try again.';
    }
    return '';
  }

  function readCache() {
    try { return JSON.parse(localStorage.getItem(CACHE_KEY) || 'null'); }
    catch (e) { return null; }
  }

  function writeCache(obj) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(obj)); }
    catch (e) { /* quota or private mode - the page still works, just slower */ }
  }

  async function fetchAll() {
    const snap = await getDocs(collection(db, 'news'));
    const items = snap.docs.map((d) => {
      const v = d.data();
      v.id = Number(d.id);
      return v;
    });
    items.sort((a, b) => String(b.first_seen).localeCompare(String(a.first_seen)));
    return items;
  }

  async function load() {
    say('Loading…');
    const cached = readCache();

    // One cheap read tells us whether the cache is still good. Without this the
    // page would re-read every row on every visit.
    let meta = null;
    try {
      const m = await getDoc(doc(db, 'meta', 'status'));
      meta = m.exists() ? m.data() : null;
    } catch (e) {
      // Rules or offline: fall through and just fetch the collection.
    }

    if (cached && meta && cached.generated_at === meta.generated_at) {
      window.__glSetData({ items: cached.items, generated_at: cached.generated_at });
      return;
    }

    const items = await fetchAll();
    const generated_at = (meta && meta.generated_at) || new Date().toISOString();
    writeCache({ generated_at, items });
    window.__glSetData({ items, generated_at });
  }

  onAuthStateChanged(auth, async (user) => {
    if (!user) {
      gate(
        '<p>Sign in to read the archive.</p>' +
        '<button id="gl-signin">Sign in with Google</button>' +
        '<p id="gl-hint" style="font-size:12px;color:#8b93a7;margin-top:14px"></p>'
      );
      say('Signed out.');
      wireSignIn(auth, GoogleAuthProvider, signInWithPopup, signInWithRedirect);
      return;
    }

    say('Signed in as ' + (user.email || user.displayName || 'user') + ' · loading…');
    try {
      await load();
    } catch (e) {
      say('Could not load news: ' + e.message);
      gate('<p>Could not read the archive.</p><p>' + String(e.message) + '</p>');
    }

    // A small sign-out affordance; the header is otherwise unchanged.
    const sub = statsEl();
    const out = document.createElement('a');
    out.href = '#';
    out.textContent = ' sign out';
    out.style.marginLeft = '8px';
    out.addEventListener('click', (ev) => { ev.preventDefault(); signOut(auth); });
    sub.appendChild(out);
  });
}
