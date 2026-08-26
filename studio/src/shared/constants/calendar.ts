/**
 * Calendar constants — working days and the timezone picker's list.
 *
 * Shared because the same three values are needed wherever hours are edited:
 * the business-calendar screen (which moved from Admin Console into the Calendar module)
 * and a person's working pattern. Copying them would let the two drift, and the
 * timezone list is long enough that a drift would go unnoticed.
 */

export const DAY_NAMES = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const ALL_DAYS  = [1, 2, 3, 4, 5, 6, 7];
export const TZ_GROUPS: Array<{ label: string; zones: string[] }> = [
  { label: "UTC", zones: ["UTC"] },
  { label: "Africa", zones: [
    "Africa/Abidjan","Africa/Accra","Africa/Addis_Ababa","Africa/Algiers","Africa/Cairo",
    "Africa/Casablanca","Africa/Johannesburg","Africa/Lagos","Africa/Nairobi","Africa/Tripoli","Africa/Tunis",
  ]},
  { label: "America", zones: [
    "America/Anchorage","America/Bogota","America/Buenos_Aires","America/Caracas",
    "America/Chicago","America/Costa_Rica","America/Denver","America/El_Salvador",
    "America/Guatemala","America/Guayaquil","America/Halifax","America/Indiana/Indianapolis",
    "America/La_Paz","America/Lima","America/Los_Angeles","America/Manaus",
    "America/Mexico_City","America/Monterrey","America/New_York","America/Panama",
    "America/Phoenix","America/Puerto_Rico","America/Santiago","America/Sao_Paulo",
    "America/St_Johns","America/Toronto","America/Vancouver",
  ]},
  { label: "Asia", zones: [
    "Asia/Almaty","Asia/Amman","Asia/Baghdad","Asia/Baku","Asia/Bangkok",
    "Asia/Beirut","Asia/Colombo","Asia/Dhaka","Asia/Dubai","Asia/Hong_Kong",
    "Asia/Irkutsk","Asia/Jakarta","Asia/Jerusalem","Asia/Kabul","Asia/Karachi",
    "Asia/Kathmandu","Asia/Kolkata","Asia/Krasnoyarsk","Asia/Kuala_Lumpur",
    "Asia/Kuwait","Asia/Macau","Asia/Magadan","Asia/Manila","Asia/Muscat",
    "Asia/Nicosia","Asia/Novosibirsk","Asia/Phnom_Penh","Asia/Rangoon",
    "Asia/Riyadh","Asia/Seoul","Asia/Shanghai","Asia/Singapore","Asia/Taipei",
    "Asia/Tashkent","Asia/Tbilisi","Asia/Tehran","Asia/Tokyo","Asia/Ulaanbaatar",
    "Asia/Vladivostok","Asia/Yakutsk","Asia/Yekaterinburg","Asia/Yerevan",
  ]},
  { label: "Atlantic", zones: [
    "Atlantic/Azores","Atlantic/Cape_Verde","Atlantic/Reykjavik",
  ]},
  { label: "Australia", zones: [
    "Australia/Adelaide","Australia/Brisbane","Australia/Darwin",
    "Australia/Hobart","Australia/Lord_Howe","Australia/Melbourne",
    "Australia/Perth","Australia/Sydney",
  ]},
  { label: "Europe", zones: [
    "Europe/Amsterdam","Europe/Athens","Europe/Belgrade","Europe/Berlin",
    "Europe/Brussels","Europe/Bucharest","Europe/Budapest","Europe/Copenhagen",
    "Europe/Dublin","Europe/Helsinki","Europe/Istanbul","Europe/Kiev",
    "Europe/Lisbon","Europe/London","Europe/Luxembourg","Europe/Madrid",
    "Europe/Minsk","Europe/Monaco","Europe/Moscow","Europe/Oslo",
    "Europe/Paris","Europe/Prague","Europe/Rome","Europe/Samara","Europe/Sofia",
    "Europe/Stockholm","Europe/Tallinn","Europe/Vienna","Europe/Vilnius",
    "Europe/Warsaw","Europe/Zurich",
  ]},
  { label: "Pacific", zones: [
    "Pacific/Auckland","Pacific/Chatham","Pacific/Fiji","Pacific/Guam",
    "Pacific/Honolulu","Pacific/Midway","Pacific/Noumea","Pacific/Pago_Pago",
    "Pacific/Port_Moresby","Pacific/Tongatapu",
  ]},
];
