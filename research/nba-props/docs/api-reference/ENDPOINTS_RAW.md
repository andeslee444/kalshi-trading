# Raw Captured Endpoints

All endpoints observed from network traffic on 2026-03-16.

## Complete Deduplicated List

| Method | Endpoint | Source Page |
|--------|----------|-------------|
| POST | `/login` | Login page |
| POST | `/tracking/web` | All pages |
| GET | `/home/nba/next?cohort=0` | Home (NBA) |
| GET | `/home/nhl/next?cohort=0` | Home (NHL) |
| GET | `/home/{sport}/boostcontrol?cohort=0&day=2026-03-16` | Home |
| GET | `/livefeed/all/feed` | Home (global) |
| GET | `/games/playerratingcontest/1548?contestType=sport&source=home` | Home (NBA) |
| GET | `/games/playerratingcontest/1550?contestType=sport&source=home` | Home (NHL) |
| GET | `/games/23454/sport/nba/feed?version=2&view=recent&viewFrame=default` | Game detail |
| GET | `/squads?sport=nba` | Home (NBA) |
| GET | `/squads?sport=nhl` | Home (NHL) |
| GET | `/predictions/game/nba/23454/markets` | Game markets tab |
| GET | `/predictions/marketembed/1943` | Market detail |
| GET | `/predictions/market/1943/positions` | Market positions |
| GET | `/predictions/openpositions` | Predictions portfolio |
| GET | `/predictions/portfolioperformance?timeframe=1w` | Predictions portfolio |
| GET | `/predictions/feed/config` | Predictions markets |
| GET | `/predictions/gamemarkets/nba` | Predictions markets tab |
| GET | `/user` | Profile |
| GET | `/user/k3LkNN1v/skins` | Profile |
| GET | `/user/k3LkNN1v/albums` | Profile |
| GET | `/getchecklistsandinfo` | Profile |
| GET | `/sportbrawluserstats/k3LkNN1v` | Profile |
| GET | `/userfeaturedcardrows/k3LkNN1v` | Profile |
| GET | `/cardalbums/k3LkNN1v` | Profile |
| GET | `/userbrawltrophies/k3LkNN1v?limit=10` | Profile |
| GET | `/collecting/k3LkNN1v/info` | Cards |
| GET | `/collectingpacks/nhl/season/2025/shopinfo` | Cards |
| GET | `/players/sport/nhl/search?includeNoOneOption=false&searchType=cardsTabUpsell&season=2025` | Cards |
| GET | `/cardmarketplaceconfiguration` | Marketplace |
| GET | `/cardmarketplacelistings?cohort=all&listingType=all&prestige=all&rarity=all&season=2025&sport=nhl` | Marketplace |
| GET | `/groups/777777777/feedadvanced` | Groups |
| GET | `/activity` | Activity |
| PUT | `/activity/clearinfo` | Activity |
| GET | `/messages/channels?type=default` | Messages |
| GET | `/homemenu` | Menu |
| GET | `/leaderboard/nhl/categories` | Leaderboards |
| GET | `/leaderboard/nhl/season/2025/type/weekly/typevalue/24?visibility=all` | Leaderboards |
| GET | `/playerboxscores/{boxscoreId}?version=2` | Player box score |
| GET | `/players/{playerId}/sport/{sport}?season={year}` | Player profile |
| GET | `/players/{playerId}/sport/{sport}/seasonfeed?limit=10&season={year}&view=recent&viewFrame=default` | Player season feed |
| GET | `/gamepacks/{gameId}/sport/{sport}` | Game packs |
| GET | `/comments/type/playerboxscore/entity/{id}?limit=25&sort=top&version=2` | Player box score comments |
| GET | `/groupunseeninfo?entityId={id}&entityType=playerboxscore` | Unseen info for entity |
| GET | `/rankings/sport/{sport}/entity/player/ranking/tertiary` | Player rankings (7-day) |
| GET | `/rankings/sport/{sport}/entity/player/ranking/secondary?season={year}` | Player rankings (30-day) |
| GET | `/rankings/sport/{sport}/entity/player/ranking/primary?season={year}` | Player rankings (season) |
| GET | `/rankings/sport/{sport}/entity/team/ranking/tertiary` | Team rankings (7-day) |
| GET | `/playerstatleaders/{sport}/seasons` | Player stat leader seasons |
| GET | `/playerstatleaders/{sport}/seasons/{year}/seasontypes/{seasonType}/stats/{statId}` | Player stat leaders |
| GET | `/teamstatleaders/{sport}/seasons` | Team stat leader seasons |
| GET | `/teamstatleaders/{sport}/seasons/{year}/seasontypes/{seasonType}/stats/{statId}` | Team stat leaders |
| GET | `/teamstandings/sport/{sport}` | Team standings config |
| GET | `/teamstandings/sport/{sport}/conference/{conference}/division/{division}?season={year}` | Division standings |
| GET | `/home/{sport}/days?type=condensed` | Game schedule/calendar |
| GET | `/stattrackers?day={YYYY-MM-DD}&sport={sport}` | Stat trackers (player props) |

## Domains Observed

| Domain | Purpose |
|--------|---------|
| `web.realapp.com` | API backend (all endpoints above) |
| `www.realapp.com` | Frontend SPA |
| `realsports.io` | CDN for JS/CSS bundles |
| `media.realapp.com` | Images, avatars, favicons |
| `unpkg.com` | Third-party libraries (react-image-crop) |

## Technologies Detected

- **Frontend**: React (SPA, single-page app)
- **Real-time**: Socket.io (bundled in JS)
- **CSS**: CSS-in-JS (inline styles, `css-175oi2r` class pattern = React Native Web)
- **Framework**: Likely React Native Web (cross-platform mobile + web)
- **Image cropping**: react-image-crop v11.0.10
