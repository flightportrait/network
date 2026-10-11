// /network/de/above, with the visitor's approximate position.
import { abovePage } from "../../_lib/above.js";

export function onRequestGet(context) {
  return abovePage(context, "/network/de/above");
}
