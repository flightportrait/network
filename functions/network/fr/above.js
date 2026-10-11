// /network/fr/above, with the visitor's approximate position.
import { abovePage } from "../../_lib/above.js";

export function onRequestGet(context) {
  return abovePage(context, "/network/fr/above");
}
