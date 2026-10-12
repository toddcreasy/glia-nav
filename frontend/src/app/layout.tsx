import type { Metadata } from "next";
import { Geist_Mono, Public_Sans, Source_Serif_4 } from "next/font/google";
import "./globals.css";
import Disclaimer from "./disclaimer";

const publicSans = Public_Sans({
  variable: "--font-public-sans",
  subsets: ["latin"],
});

const sourceSerif = Source_Serif_4({
  variable: "--font-source-serif",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "GliaNav",
  description:
    "An LLM-assisted navigator for glioblastoma clinical trials and research literature.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      data-theme="light"
      suppressHydrationWarning
      className={`${publicSans.variable} ${sourceSerif.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        {/* Runs before first paint: the saved choice, else the system setting. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem("theme");if(t!=="light"&&t!=="dark")t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.setAttribute("data-theme",t)}catch(e){}})()`,
          }}
        />
      </head>
      <body className="min-h-full flex flex-col">
        <main className="flex-1">{children}</main>
        <Disclaimer />
      </body>
    </html>
  );
}
