import { redirect } from "next/navigation";

export default function BacktestPage(): never {
  redirect("/analysis?compare=1&s=trade_strat_v1&s2=trade_strat_v2");
}
