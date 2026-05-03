import { redirect } from "next/navigation";

export default function BacktestPage(): never {
  redirect("/analysis?compare=1&s=strat_30k&s2=strat_15k");
}
