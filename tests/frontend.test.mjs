import {test} from 'node:test';
import assert from 'node:assert/strict';

const elements = new Map();
globalThis.HTMLElement = class {
  attachShadow() {
    this.shadowRoot = {children: [], replaceChildren(...children) {this.children = children;}};
  }
};
globalThis.customElements = {whenDefined: async () => {}, get: name => elements.get(name), define: (name, cls) => elements.set(name,cls)};
const cards = [];
globalThis.window = {loadCardHelpers: async () => ({createCardElement: config => {
  const properties = new Map();
  const card = {config, properties, style: {setProperty: (key,value) => properties.set(key,value)},
    getCardSize: () => 4, getGridOptions: () => ({columns: 6, rows: 4})};
  cards.push(card);
  return card;
}})};
const {thermostatColor, HeatingPulseThermostatCard} = await import('../custom_components/heating_pulse_controller_v2/www/thermostat-card.js');

test('colour follows heating/idle/off and treats absent states as unknown', () => {
  const s = action => ({state:'heat',attributes:{hvac_action:action}});
  assert.equal(thermostatColor(s('heating')), 'var(--deep-orange-color, #ff6f22)');
  assert.match(thermostatColor(s('idle')), /blue/);
  assert.match(thermostatColor(s('off')), /grey/);
  assert.match(thermostatColor(s(null)), /grey/);
  assert.match(thermostatColor(undefined), /unavailable/);
  assert.match(thermostatColor({state:'unavailable',attributes:{hvac_action:'heating'}}), /unavailable/);
});
test('wrapper preserves thermostat features and changes CSS without changing modes or config', async () => {
  const card = new HeatingPulseThermostatCard();
  card.setConfig({type:'custom:heating-pulse-thermostat-card',entity:'climate.test',features:[{type:'climate-hvac-modes'}]});
  await card._ready;
  for (const [action,color] of [['heating','orange'],['idle','blue'],['off','grey']]) {
    const hass = {states:{'climate.test':{state:'heat',attributes:{hvac_action:action}}}};
    card.hass = hass;
    assert.equal(card._card.hass,hass);
    assert.match(card._card.properties.get('--state-climate-heat-color'),new RegExp(color));
    assert.equal(hass.states['climate.test'].state,'heat');
  }
  assert.equal(card._card.config.type,'thermostat');
  assert.equal(card._card.config.features[0].type,'climate-hvac-modes');
  assert.equal(card.getCardSize(),4);
  assert.deepEqual(card.getGridOptions(),{columns:6,rows:4});
});
test('rejects non-climate entities', () => {
  assert.throws(() => new HeatingPulseThermostatCard().setConfig({entity:'sensor.room'}));
});
test('latest config wins an asynchronous creation race', async () => {
  const pending = [];
  const normal = window.loadCardHelpers;
  window.loadCardHelpers = () => new Promise(resolve => pending.push(resolve));
  const card = new HeatingPulseThermostatCard();
  card.setConfig({entity:'climate.old'});
  card.setConfig({entity:'climate.new'});
  pending[1](await normal()); await card._ready;
  pending[0](await normal()); await Promise.resolve();
  assert.equal(card._card.config.entity,'climate.new');
  window.loadCardHelpers = normal;
});

test('early extra-module loading waits for HA to replace its custom-element registry', async () => {
  const oldRegistry = globalThis.customElements;
  const oldElement = globalThis.HTMLElement;
  const oldPicker = window.customCards;
  let ready;
  let entered;
  const waiting = new Promise(resolve => {entered = resolve;});
  let earlyDefinitions = 0;
  globalThis.customElements = {
    whenDefined: name => {
      assert.equal(name, 'home-assistant');
      entered();
      return new Promise(resolve => {ready = resolve;});
    },
    get: () => undefined,
    define: () => {earlyDefinitions++;}
  };
  try {
    const loading = import('../custom_components/heating_pulse_controller_v2/www/thermostat-card.js?startup-race');
    await waiting;
    assert.equal(earlyDefinitions, 0);
    const frontendRegistry = new Map();
    class FrontendElement extends oldElement {}
    globalThis.HTMLElement = FrontendElement;
    globalThis.customElements = {
      get: name => frontendRegistry.get(name),
      define: (name, cls) => frontendRegistry.set(name, cls)
    };
    ready();
    const module = await loading;
    assert.equal(frontendRegistry.get('heating-pulse-thermostat-card'), module.HeatingPulseThermostatCard);
    assert.equal(Object.getPrototypeOf(module.HeatingPulseThermostatCard), FrontendElement);
    assert.equal(earlyDefinitions, 0);
  } finally {
    globalThis.customElements = oldRegistry;
    globalThis.HTMLElement = oldElement;
    window.customCards = oldPicker;
  }
});

test('frost heating is red and native presentation remains Off without mutating HA state', async () => {
  const card = new HeatingPulseThermostatCard();
  card.setConfig({entity:'climate.frost',features:[{type:'climate-hvac-modes'}]});
  await card._ready;
  const state = {entity_id:'climate.frost',state:'off',attributes:{hvac_action:'heating',frost_protection_active:true,temperature:10,heating_target_temperature:23}};
  const hass = {states:{'climate.frost':state}};
  card.hass = hass;
  assert.match(thermostatColor(state), /red/);
  assert.equal(card._card.hass.states['climate.frost'].state,'off');
  assert.equal(card._card.hass.states['climate.frost'].attributes.hvac_action,'off');
  assert.equal(state.attributes.hvac_action,'heating');
  assert.equal(state.attributes.temperature,10);
  assert.equal(state.attributes.heating_target_temperature,23);
  assert.match(card._card.properties.get('--primary-text-color'),/red/);
  assert.equal(card._card.config.features[0].type,'climate-hvac-modes');
  const heating = {...state,state:'heat'};
  card.hass = {states:{'climate.frost':heating}};
  assert.match(thermostatColor(heating),/orange/);
  assert.equal(card._card.properties.get('--primary-text-color'),'');
});

test('Antifreeze label is scoped and restores the original native label', async () => {
  const previousDocument = globalThis.document;
  globalThis.document = {createElement: tag => ({tag})};
  try {
    const card = new HeatingPulseThermostatCard();
    card.setConfig({entity:'climate.frost'});
    await card._ready;
    const control = {_renderLabel() { return 'native label'; }, requestUpdate() {this.updates=(this.updates||0)+1;}};
    card._card.shadowRoot = {querySelector: () => control};
    card.hass = {states:{'climate.frost':{state:'off',attributes:{hvac_action:'heating',frost_protection_active:true}}}};
    await card._presentationReady;
    assert.equal(control._renderLabel().textContent,'Antifreeze');
    assert.equal(control._renderLabel().className,'label');
    card.hass = {states:{'climate.frost':{state:'off',attributes:{hvac_action:'off',frost_protection_active:true}}}};
    await card._presentationReady;
    assert.equal(control._renderLabel(),'native label');
    assert.equal(control.updates,2);
  } finally {globalThis.document=previousDocument;}
});
