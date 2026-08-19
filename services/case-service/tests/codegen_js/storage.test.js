// Runs the GENERATED src/api/storage.ts on node with the three native modules
// stubbed, so the iOS/Android branch — the one a browser can never take — is
// actually executed rather than reasoned about.
const Module = require('module');
const path = require('path');

function run(platformOS) {
  const async_ = new Map();
  const keystore = new Map();

  const stubs = {
    'react-native': { Platform: { OS: platformOS } },
    'expo-secure-store': {
      __esModule: true,
      getItemAsync: async k => (keystore.has(k) ? keystore.get(k) : null),
      setItemAsync: async (k, v) => { keystore.set(k, v); },
      deleteItemAsync: async k => { keystore.delete(k); },
    },
    '@react-native-async-storage/async-storage': {
      __esModule: true,
      default: {
        getItem: async k => (async_.has(k) ? async_.get(k) : null),
        setItem: async (k, v) => { async_.set(k, v); },
        removeItem: async k => { async_.delete(k); },
      },
    },
  };

  const orig = Module._load;
  Module._load = function (req, parent, isMain) {
    if (stubs[req]) return stubs[req];
    return orig.apply(this, arguments);
  };
  delete require.cache[require.resolve(process.env.GENERATED_OUT + '/storage.js')];
  const mod = require(process.env.GENERATED_OUT + '/storage.js');
  Module._load = orig;
  return { mod, async_, keystore };
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log('  PASS  ' + name); }
  else { failures++; console.log('  FAIL  ' + name + (extra ? '  ' + JSON.stringify(extra) : '')); }
}

(async () => {
  console.log('ios / android — the keystore branch');
  {
    const { mod, async_, keystore } = run('ios');
    await mod.setSecret(mod.K.token, 'TOKEN-VALUE');
    check('token goes to the keystore', keystore.get('velaris_token') === 'TOKEN-VALUE');
    check('token is NOT in AsyncStorage', !async_.has('velaris_token'), [...async_.keys()]);

    await mod.setPlain(mod.K.baseUrl, 'https://velaris.example.com');
    check('base URL stays in plain storage', async_.get('velaris_base_url') === 'https://velaris.example.com');
    check('base URL is not in the keystore', !keystore.has('velaris_base_url'));

    check('reads back', (await mod.getSecret(mod.K.token)) === 'TOKEN-VALUE');

    await mod.deleteSecret(mod.K.token);
    check('sign-out clears the keystore copy', !keystore.has('velaris_token'));
  }

  console.log('upgrade from a build that stored the token in the clear');
  {
    const { mod, async_, keystore } = run('android');
    async_.set('velaris_token', 'OLD-PLAINTEXT-TOKEN');   // what the previous build left behind
    const got = await mod.getSecret(mod.K.token);
    check('the old token is still usable', got === 'OLD-PLAINTEXT-TOKEN');
    check('it has moved into the keystore', keystore.get('velaris_token') === 'OLD-PLAINTEXT-TOKEN');
    check('the plaintext copy is gone', !async_.has('velaris_token'), [...async_.keys()]);
  }

  console.log('upgrade from the pre-rebrand key name');
  {
    const { mod, async_, keystore } = run('ios');
    async_.set('helix_token', 'LEGACY-TOKEN');
    const got = await mod.getSecret(mod.K.token);
    check('helix_token is carried over', got === 'LEGACY-TOKEN');
    check('and lands in the keystore', keystore.get('velaris_token') === 'LEGACY-TOKEN');
    check('no plaintext copy under either name', !async_.has('helix_token') && !async_.has('velaris_token'), [...async_.keys()]);
  }

  console.log('sign-out must clear every copy, not just the newest');
  {
    const { mod, async_, keystore } = run('ios');
    keystore.set('velaris_token', 'IN-KEYSTORE');
    async_.set('velaris_token', 'STRAY-PLAINTEXT');   // e.g. left by a half-finished upgrade
    await mod.deleteSecret(mod.K.token);
    check('keystore cleared', !keystore.has('velaris_token'));
    check('stray plaintext cleared too', !async_.has('velaris_token'), [...async_.keys()]);
  }

  console.log('web — no keystore exists, so it falls back and says so in the README');
  {
    const { mod, async_, keystore } = run('web');
    await mod.setSecret(mod.K.token, 'WEB-TOKEN');
    check('keystore untouched on web', keystore.size === 0);
    check('value still retrievable', (await mod.getSecret(mod.K.token)) === 'WEB-TOKEN');
  }

  console.log(failures === 0 ? '\nALL PASS' : '\n' + failures + ' FAILED');
  process.exit(failures ? 1 : 0);
})();
